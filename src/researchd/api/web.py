"""Loopback-only HTTP client facade over the in-process LocalControlAPI."""
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from researchd.api.agui import AGUIProjectionAdapter
from researchd.api.control import LocalControlAPI
from researchd.daemon.runtime import ResearchDaemon
from researchd.domain.base import DomainModel
from researchd.runtime_sessions.contracts import (
    RuntimeSessionAttachCommand,
    RuntimeSessionStartCommand,
    RuntimeSessionStopCommand,
)


class _ControlCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CancelRunCommand(_ControlCommand):
    pass


class ApproveWorkOrderCommand(_ControlCommand):
    grant_id: str = Field(min_length=1, max_length=128)


class ResolveHumanCommand(_ControlCommand):
    action: Literal["abort", "revise"]
    objective: str | None = Field(default=None, min_length=1, max_length=16_384)

    @model_validator(mode="after")
    def revision_requires_objective(self) -> "ResolveHumanCommand":
        if self.action == "revise" and self.objective is None:
            raise ValueError("revision objective is required")
        return self


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

    def __init__(self, api: LocalControlAPI, daemon: ResearchDaemon | None = None) -> None:
        self.api = api
        self.daemon = daemon

    async def post(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        parts = [unquote(item) for item in urlparse(path).path.split("/") if item]
        if parts == ["api", "runtime-sessions", "start"]:
            return 200, self._execute(RuntimeSessionStartCommand.model_validate(payload))
        if parts == ["api", "runtime-sessions", "attach"]:
            return 200, self._execute(RuntimeSessionAttachCommand.model_validate(payload))
        if len(parts) == 4 and parts[:2] == ["api", "runtime-sessions"] and parts[3] == "stop":
            command = RuntimeSessionStopCommand.model_validate({
                **payload,
                "runtime_session_id": parts[2],
            })
            return 200, self._execute(command)
        if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "cancel":
            CancelRunCommand.model_validate(payload)
            return 200, await self.api.cancel_run(parts[2])
        if len(parts) == 4 and parts[:2] == ["api", "work-orders"] and parts[3] == "approve":
            approval = ApproveWorkOrderCommand.model_validate(payload)
            return 200, await self.api.approve(parts[2], approval.grant_id)
        if len(parts) == 4 and parts[:2] == ["api", "work-orders"] and parts[3] == "human-decision":
            decision = ResolveHumanCommand.model_validate(payload)
            return 200, self.api.resolve_human(parts[2], action=decision.action, objective=decision.objective)
        return 404, {"error": "unknown command"}

    def _execute(self, command: DomainModel) -> dict[str, Any]:
        if self.daemon is None:
            raise RuntimeError("runtime mutation requires researchd")
        result = self.daemon.execute(command)
        if not isinstance(result, BaseModel):
            raise TypeError("daemon command result must be a typed model")
        return result.model_dump(mode="json")


def make_handler(
    api: LocalControlAPI,
    daemon: ResearchDaemon | None = None,
) -> type[BaseHTTPRequestHandler]:
    router = ControlResourceRouter(api)
    commands = ControlCommandRouter(api, daemon)
    projection = AGUIProjectionAdapter(api)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            parts = [unquote(item) for item in parsed.path.split("/") if item]
            if parts == ["api", "health"] and daemon is not None:
                self._send_json(200 if daemon.health()["ready"] else 503, daemon.health())
                return
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "stream":
                self._send_event_stream(parts[2], parse_qs(parsed.query))
                return
            status, payload = router.get(self.path)
            self._send_json(status, payload)

        def do_POST(self) -> None:  # noqa: N802
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
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("control HTTP server must bind loopback")
    server = ThreadingHTTPServer((host, port), make_handler(api, daemon))
    return server


__all__ = ["ControlCommandRouter", "ControlResourceRouter", "make_handler", "serve_local_control"]
