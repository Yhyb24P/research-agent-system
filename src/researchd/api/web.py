"""Loopback-only HTTP client facade over the in-process LocalControlAPI."""
import asyncio
import inspect
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import BaseModel, ValidationError

from researchd.api.agui import AGUIProjectionAdapter
from researchd.api.control import LocalControlAPI
from researchd.daemon.runtime import ResearchDaemon
from researchd.daemon.contracts import (
    ExternalHumanDecisionRequest,
    ExternalRunCancelRequest,
    ExternalWorkOrderApproveRequest,
    HumanDecisionCommand,
    RunCancelCommand,
    WorkOrderApproveCommand,
)
from researchd.domain.base import DomainModel
from researchd.domain.ids import RuntimeSessionId
from researchd.runtime_sessions.contracts import (
    ExternalRuntimeSessionAttachRequest,
    ExternalRuntimeSessionStartRequest,
    ExternalRuntimeSessionStopRequest,
    ResolvedProcessLaunch,
    ResolvedRemoteHttpLaunch,
    RuntimeSessionAttachCommand,
    RuntimeSessionStartCommand,
    RuntimeSessionStopCommand,
)


class RuntimeLaunchProfileAuthority(Protocol):
    def resolve_process(self, runtime_id: str) -> ResolvedProcessLaunch: ...
    def resolve_remote_http(self, runtime_id: str) -> ResolvedRemoteHttpLaunch: ...


class ControlResourceRouter:
    """Pure resource router shared by HTTP and tests; it owns no state."""
    def __init__(self, api: LocalControlAPI) -> None:
        self.api = api

    def get(self, path: str) -> tuple[int, dict[str, Any] | list[dict[str, Any]]]:
        parts = [unquote(item) for item in urlparse(path).path.split("/") if item]
        query = parse_qs(urlparse(path).query)
        try:
            if parts == ["api", "agents"]:
                return 200, self.api.agents()
            if len(parts) == 3 and parts[:2] == ["api", "agents"]:
                return 200, self.api.agent(parts[2])
            if parts == ["api", "delegations"]:
                return 200, self.api.delegations(query.get("run", [None])[0])
            if parts == ["api", "approvals"]:
                return 200, self.api.approvals(query.get("run", [None])[0])
            if parts == ["api", "workspace-grants"]:
                return 200, self.api.workspace_grants(query.get("run", [None])[0])
            if parts == ["api", "runtime-sessions"]:
                return 200, self.api.runtime_sessions(query.get("runtime", [None])[0])
            if parts == ["api", "daemon-commands"]:
                return 200, self.api.daemon_commands(query.get("status", [None])[0])
            if parts == ["api", "system-events"]:
                raw_offset = query.get("after", [None])[0]
                offset = int(raw_offset) if raw_offset is not None else None
                if offset is not None and offset < 0:
                    raise ValueError("stream offset must be nonnegative")
                return 200, {"events": self.api.system_events(after_stream_offset=offset)}
            if parts == ["api", "artifacts"] and query.get("run", [None])[0]:
                return 200, self.api.artifacts(query["run"][0])
            if len(parts) == 3 and parts[:2] == ["api", "timeline"]:
                return 200, self.api.timeline(parts[2])
            if len(parts) == 3 and parts[:2] == ["api", "delegations"]:
                return 200, self.api.delegation(parts[2])
            if len(parts) == 3 and parts[:2] == ["api", "runs"] and parts[2]:
                return 200, self.api.run_status(parts[2])
            if parts == ["api", "runs"]:
                return 200, self.api.runs()
            if len(parts) == 3 and parts[:2] == ["api", "events"] and parts[2]:
                raw_offset = query.get("after", [None])[0]
                offset = int(raw_offset) if raw_offset is not None else None
                if offset is not None and offset < 0:
                    raise ValueError("stream offset must be nonnegative")
                return 200, {"events": self.api.events(parts[2], after_stream_offset=offset)}
        except LookupError:
            return 404, {"error": "not found"}
        except ValueError:
            return 400, {"error": "invalid stream offset"}
        return 404, {"error": "unknown resource"}


class ControlCommandRouter:
    """Narrow command adapter; arbitrary UI events have no mutation path."""

    def __init__(
        self,
        api: LocalControlAPI,
        daemon: ResearchDaemon | None = None,
        *,
        human_actor_id: str = "local-control-client",
        launch_profiles: RuntimeLaunchProfileAuthority | None = None,
    ) -> None:
        self.api = api
        self.daemon = daemon
        self.human_actor_id = human_actor_id
        self.launch_profiles = launch_profiles

    async def post(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        parts = [unquote(item) for item in urlparse(path).path.split("/") if item]
        if parts == ["api", "runtime-sessions", "start"]:
            start_request = ExternalRuntimeSessionStartRequest.model_validate(payload)
            start_launch = self._launch_profiles().resolve_process(str(start_request.runtime_id))
            return await self._execute(RuntimeSessionStartCommand(
                command_id=start_request.command_id,
                runtime_session_id=start_request.runtime_session_id,
                runtime_id=start_request.runtime_id,
                actor_type="HUMAN",
                actor_id=self.human_actor_id,
                launch_spec=start_launch.launch_spec,
                launch_profile_sha256=start_launch.spec_sha256,
            ))
        if parts == ["api", "runtime-sessions", "attach"]:
            attach_request = ExternalRuntimeSessionAttachRequest.model_validate(payload)
            attach_launch = self._launch_profiles().resolve_remote_http(
                str(attach_request.runtime_id)
            )
            return await self._execute(RuntimeSessionAttachCommand(
                command_id=attach_request.command_id,
                runtime_session_id=attach_request.runtime_session_id,
                runtime_id=attach_request.runtime_id,
                actor_type="HUMAN",
                actor_id=self.human_actor_id,
                launch_spec=attach_launch.launch_spec,
                launch_profile_sha256=attach_launch.spec_sha256,
            ))
        if len(parts) == 4 and parts[:2] == ["api", "runtime-sessions"] and parts[3] == "stop":
            stop_request = ExternalRuntimeSessionStopRequest.model_validate(payload)
            stop_command = RuntimeSessionStopCommand(
                command_id=stop_request.command_id,
                runtime_session_id=RuntimeSessionId(parts[2]),
                runtime_id=stop_request.runtime_id,
                actor_type="HUMAN",
                actor_id=self.human_actor_id,
                expected_version=stop_request.expected_version,
            )
            return await self._execute(stop_command)
        if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "cancel":
            cancel_request = ExternalRunCancelRequest.model_validate(payload)
            cancel_command = RunCancelCommand(
                command_id=cancel_request.command_id,
                actor_type="HUMAN",
                actor_id=self.human_actor_id,
                run_id=parts[2],
            )
            return await self._execute(cancel_command)
        if len(parts) == 4 and parts[:2] == ["api", "work-orders"] and parts[3] == "approve":
            approve_request = ExternalWorkOrderApproveRequest.model_validate(payload)
            approve_command = WorkOrderApproveCommand(
                command_id=approve_request.command_id,
                actor_type="HUMAN",
                actor_id=self.human_actor_id,
                work_order_id=parts[2],
                grant_id=approve_request.grant_id,
            )
            return await self._execute(approve_command)
        if len(parts) == 4 and parts[:2] == ["api", "work-orders"] and parts[3] == "human-decision":
            decision_request = ExternalHumanDecisionRequest.model_validate(payload)
            decision_command = HumanDecisionCommand(
                command_id=decision_request.command_id,
                actor_type="HUMAN",
                actor_id=self.human_actor_id,
                work_order_id=parts[2],
                action=decision_request.action,
                objective=decision_request.objective,
            )
            return await self._execute(decision_command)
        return 404, {"error": "unknown command"}

    async def _execute(self, command: DomainModel) -> tuple[int, dict[str, Any]]:
        if self.daemon is None:
            raise RuntimeError("mutation requires researchd")
        result = self.daemon.execute(command)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, BaseModel):
            raise TypeError("daemon command result must be a typed model")
        response = result.model_dump(mode="json")
        return (409 if response.get("status") == "REJECTED" else 202), response

    def _launch_profiles(self) -> RuntimeLaunchProfileAuthority:
        if self.launch_profiles is None:
            raise RuntimeError("runtime launch profile authority is not configured")
        return self.launch_profiles


def make_handler(
    api: LocalControlAPI,
    daemon: ResearchDaemon | None = None,
    *,
    control_token: str | None = None,
    launch_profiles: RuntimeLaunchProfileAuthority | None = None,
) -> type[BaseHTTPRequestHandler]:
    router = ControlResourceRouter(api)
    commands = ControlCommandRouter(api, daemon, launch_profiles=launch_profiles)
    projection = AGUIProjectionAdapter(api)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            parts = [unquote(item) for item in parsed.path.split("/") if item]
            if parts == ["api", "health"] and daemon is not None:
                self._send_json(200 if daemon.health()["ready"] else 503, daemon.health())
                return
            if not self._authorized():
                return
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "stream":
                self._send_event_stream(parts[2], parse_qs(parsed.query))
                return
            status, payload = router.get(self.path)
            self._send_json(status, payload)

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length < 0 or content_length > 65_536:
                    self._send_json(413, {"error": "command payload too large"})
                    return
                raw_body = self.rfile.read(content_length)
                decoded = json.loads(raw_body) if raw_body else {}
                if not isinstance(decoded, dict):
                    raise ValueError("command payload must be an object")
                status, payload = asyncio.run(commands.post(self.path, decoded))
                self._send_json(status, payload)
            except ValidationError as error:
                self._send_json(422, {"error": "invalid command", "details": error.errors(include_url=False)})
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                self._send_json(400, {"error": "invalid command payload"})
            except LookupError:
                self._send_json(404, {"error": "not found"})
            except RuntimeError as error:
                self._send_json(409, {"error": str(error)})

        def _send_json(self, status: int, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if control_token is None:
                return True
            authorization = self.headers.get("Authorization", "")
            prefix = "Bearer "
            presented = authorization[len(prefix):] if authorization.startswith(prefix) else ""
            if presented and hmac.compare_digest(presented, control_token):
                return True
            body = b'{"error": "authentication required"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("WWW-Authenticate", "Bearer")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False

        def _send_event_stream(self, run_id: str, query: dict[str, list[str]]) -> None:
            try:
                raw_offset = query.get("after", [self.headers.get("Last-Event-ID")])[0]
                offset = None if raw_offset is None or raw_offset == "" else int(raw_offset)
                if offset is not None and offset < 0:
                    raise ValueError("stream offset must be nonnegative")
                events = projection.replay(run_id, after_stream_offset=offset)
            except ValueError:
                self._send_json(400, {"error": "invalid stream offset"})
                return
            except LookupError:
                self._send_json(404, {"error": "not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            follow = query.get("follow", ["0"])[0].lower() in {"1", "true", "yes"}
            self.send_header("Connection", "keep-alive" if follow else "close")
            self.end_headers()
            current = offset or 0
            try:
                for item in events:
                    self.wfile.write(item.as_sse())
                    current = max(current, item.stream_offset)
                self.wfile.flush()
                while follow:
                    time.sleep(0.5)
                    tail = projection.replay(run_id, after_stream_offset=current)
                    if not tail:
                        self.wfile.write(b": keep-alive\n\n")
                    for item in tail:
                        self.wfile.write(item.as_sse())
                        current = item.stream_offset
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            finally:
                if not follow:
                    self.close_connection = True

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def serve_local_control(
    api: LocalControlAPI,
    *,
    daemon: ResearchDaemon | None = None,
    host: str = "127.0.0.1",
    port: int = 8788,
    control_token: str | None = None,
    launch_profiles: RuntimeLaunchProfileAuthority | None = None,
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("control HTTP server must bind loopback")
    if daemon is not None and control_token is None:
        raise ValueError("mutable control server requires a local credential")
    server = ThreadingHTTPServer(
        (host, port),
        make_handler(
            api,
            daemon,
            control_token=control_token,
            launch_profiles=launch_profiles,
        ),
    )
    return server


__all__ = ["ControlCommandRouter", "ControlResourceRouter", "make_handler", "serve_local_control"]
