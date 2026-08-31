"""Loopback-only HTTP client facade over the in-process LocalControlAPI."""
import asyncio
import inspect
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import BaseModel, ValidationError

from researchd.api.agui import AGUIProjectionAdapter
from researchd.api.browser_assets import BROWSER_ASSETS
from researchd.api.control import LocalControlAPI
from researchd.daemon.runtime import ResearchDaemon
from researchd.daemon.contracts import (
    BackupCreateCommand,
    BackupVerifyCommand,
    CollaborationMessageSendCommand,
    DaemonCommandResolveCommand,
    ExternalBackupCreateRequest,
    ExternalBackupVerifyRequest,
    ExternalCollaborationMessageSendRequest,
    ExternalDaemonCommandResolveRequest,
    ExternalHumanDecisionRequest,
    ExternalHandoffDecisionRequest,
    ExternalManagedAgentStartRequest,
    ExternalRemoteAgentAttachRequest,
    ExternalRemoteAgentDetachRequest,
    ExternalRemoteAgentRenewRequest,
    ExternalResearchTaskCreateRequest,
    ExternalRestorePlanRequest,
    ExternalRunCancelRequest,
    ExternalWorkOrderApproveRequest,
    ExternalWorkOrderRejectRequest,
    ExternalWorkspaceCreateRequest,
    HumanDecisionCommand,
    HandoffDecisionCommand,
    ManagedAgentStartCommand,
    RemoteAgentAttachCommand,
    RemoteAgentDetachCommand,
    RemoteAgentRenewCommand,
    ResearchTaskCreateCommand,
    RestorePlanCommand,
    RunCancelCommand,
    WorkOrderApproveCommand,
    WorkOrderRejectCommand,
    WorkspaceCreateCommand,
)
from researchd.daemon.reconciliation import DaemonCommandResolutionService
from researchd.domain.base import DomainModel
from researchd.domain.ids import RuntimeSessionId
from researchd.runtime_sessions.contracts import (
    ExternalRuntimeSessionStopRequest,
    RuntimeSessionStopCommand,
)


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
            if len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] == "console":
                return 200, self.api.agent_console(parts[2], run_id=query.get("run", [None])[0])
            if parts == ["api", "delegations"]:
                return 200, self.api.delegations(query.get("run", [None])[0])
            if parts == ["api", "handoffs"]:
                return 200, self.api.handoffs(query.get("run", [None])[0])
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
            if len(parts) == 3 and parts[:2] == ["api", "collaboration-messages"]:
                return 200, {"message": self.api.collaboration_message(parts[2])}
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "messages":
                return 200, {"messages": self.api.collaboration_messages(parts[2])}
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
        resolution: DaemonCommandResolutionService | None = None,
    ) -> None:
        self.api = api
        self.daemon = daemon
        self.human_actor_id = human_actor_id
        self.resolution = resolution

    async def post(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        parts = [unquote(item) for item in urlparse(path).path.split("/") if item]
        if parts == ["api", "remote-agents", "attach"]:
            attach_request = ExternalRemoteAgentAttachRequest.model_validate(payload)
            return await self._execute(RemoteAgentAttachCommand(command_id=attach_request.command_id, actor_type="HUMAN", actor_id=self.human_actor_id, runtime_id=attach_request.runtime_id))
        if parts == ["api", "remote-agents", "detach"]:
            detach_request = ExternalRemoteAgentDetachRequest.model_validate(payload)
            return await self._execute(RemoteAgentDetachCommand(command_id=detach_request.command_id, actor_type="HUMAN", actor_id=self.human_actor_id, runtime_id=detach_request.runtime_id))
        if parts == ["api", "remote-agents", "renew"]:
            renew_request = ExternalRemoteAgentRenewRequest.model_validate(payload)
            return await self._execute(RemoteAgentRenewCommand(command_id=renew_request.command_id, actor_type="HUMAN", actor_id=self.human_actor_id, runtime_id=renew_request.runtime_id))
        if len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] == "start":
            start_request = ExternalManagedAgentStartRequest.model_validate(payload)
            start_command = ManagedAgentStartCommand(
                command_id=start_request.command_id,
                actor_type="HUMAN",
                actor_id=self.human_actor_id,
                agent_id=parts[2],
                runtime_id=start_request.runtime_id,
            )
            return await self._execute(start_command)
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
        if len(parts) == 4 and parts[:2] == ["api", "handoffs"] and parts[3] == "decision":
            handoff_request = ExternalHandoffDecisionRequest.model_validate(payload)
            handoff_command = HandoffDecisionCommand(
                command_id=handoff_request.command_id,
                actor_type="HUMAN",
                actor_id=self.human_actor_id,
                proposal_id=parts[2],
                decision=handoff_request.decision,
                reason=handoff_request.reason,
                target_agent_id=handoff_request.target_agent_id,
            )
            return await self._execute(handoff_command)
        if parts == ["api", "workspaces"]:
            workspace_request = ExternalWorkspaceCreateRequest.model_validate(payload)
            workspace_command = WorkspaceCreateCommand(
                command_id=workspace_request.command_id,
                actor_type="HUMAN",
                actor_id=self.human_actor_id,
                workspace_id=workspace_request.workspace_id,
                name=workspace_request.name,
            )
            return await self._execute(workspace_command)
        if parts == ["api", "runs"]:
            task_request = ExternalResearchTaskCreateRequest.model_validate(payload)
            task_command = ResearchTaskCreateCommand(
                command_id=task_request.command_id,
                actor_type="HUMAN",
                actor_id=self.human_actor_id,
                workspace_id=task_request.workspace_id,
                objective=task_request.objective,
                run_id=task_request.run_id,
            )
            return await self._execute(task_command)
        if len(parts) == 4 and parts[:2] == ["api", "work-orders"] and parts[3] == "reject":
            reject_request = ExternalWorkOrderRejectRequest.model_validate(payload)
            reject_command = WorkOrderRejectCommand(
                command_id=reject_request.command_id,
                actor_type="HUMAN",
                actor_id=self.human_actor_id,
                work_order_id=parts[2],
                approval_id=reject_request.approval_id,
            )
            return await self._execute(reject_command)
        if parts == ["api", "collaboration-messages"]:
            message_request = ExternalCollaborationMessageSendRequest.model_validate(payload)
            message_command = CollaborationMessageSendCommand(
                command_id=message_request.command_id,
                actor_type="HUMAN",
                actor_id=self.human_actor_id,
                message_id=message_request.message_id,
                run_id=message_request.run_id,
                work_order_id=message_request.work_order_id,
                delegation_id=message_request.delegation_id,
                invocation_id=message_request.invocation_id,
                reply_to_message_id=message_request.reply_to_message_id,
                recipient_agent_id=message_request.recipient_agent_id,
                purpose=message_request.purpose,
                body=message_request.body,
                classification=message_request.classification,
            )
            return await self._execute(message_command)
        if parts == ["api", "backups", "create"]:
            backup_request = ExternalBackupCreateRequest.model_validate(payload)
            backup_command = BackupCreateCommand(
                command_id=backup_request.command_id,
                actor_type="HUMAN",
                actor_id=self.human_actor_id,
                destination=backup_request.destination,
                candidate_commit=backup_request.candidate_commit,
                candidate_tag=backup_request.candidate_tag,
            )
            return await self._execute(backup_command)
        if parts == ["api", "backups", "verify"]:
            verify_request = ExternalBackupVerifyRequest.model_validate(payload)
            verify_command = BackupVerifyCommand(
                command_id=verify_request.command_id,
                actor_type="HUMAN",
                actor_id=self.human_actor_id,
                snapshot=verify_request.snapshot,
            )
            return await self._execute(verify_command)
        if parts == ["api", "restores", "plan"]:
            restore_request = ExternalRestorePlanRequest.model_validate(payload)
            restore_command = RestorePlanCommand(
                command_id=restore_request.command_id,
                actor_type="HUMAN",
                actor_id=self.human_actor_id,
                snapshot=restore_request.snapshot,
                database_destination=restore_request.database_destination,
                artifact_destination=restore_request.artifact_destination,
                expected_candidate_commit=restore_request.expected_candidate_commit,
                expected_candidate_tag=restore_request.expected_candidate_tag,
            )
            return await self._execute(restore_command)
        if len(parts) == 4 and parts[:2] == ["api", "daemon-commands"] and parts[3] == "resolve":
            resolve_request = ExternalDaemonCommandResolveRequest.model_validate(payload)
            resolve_command = DaemonCommandResolveCommand(
                command_id=resolve_request.command_id,
                actor_type="HUMAN",
                actor_id=self.human_actor_id,
                target_command_id=parts[2],
                resource_ref=resolve_request.resource_ref,
                abandon=resolve_request.abandon,
            )
            return self._resolve(resolve_command)
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

    def _resolve(self, command: DaemonCommandResolveCommand) -> tuple[int, dict[str, Any]]:
        # Narrow recovery channel: it is deliberately reachable while the
        # daemon is non-ready, because an unknown receipt is what blocks READY.
        # It stays behind the same Bearer authentication as every other route.
        if self.resolution is None:
            raise RuntimeError("receipt resolution is not configured")
        result = self.resolution.resolve(command)
        response = result.model_dump(mode="json")
        return (409 if response.get("status") == "REJECTED" else 202), response


def make_handler(
    api: LocalControlAPI,
    daemon: ResearchDaemon | None = None,
    *,
    control_token: str | None = None,
    resolution: DaemonCommandResolutionService | None = None,
) -> type[BaseHTTPRequestHandler]:
    router = ControlResourceRouter(api)
    commands = ControlCommandRouter(
        api,
        daemon,
        resolution=resolution,
    )
    projection = AGUIProjectionAdapter(api)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            asset = BROWSER_ASSETS.get(parsed.path)
            if asset is not None:
                self._send_asset(*asset)
                return
            parts = [unquote(item) for item in parsed.path.split("/") if item]
            if parts == ["api", "health"] and daemon is not None:
                self._send_json(200 if daemon.health()["ready"] else 503, daemon.health())
                return
            if not self._authorized():
                return
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "stream":
                self._send_event_stream(parts[2], parse_qs(parsed.query))
                return
            if parts == ["api", "system-stream"]:
                self._send_system_stream(parse_qs(parsed.query))
                return
            status, payload = router.get(self.path)
            self._send_json(status, payload)

        def do_POST(self) -> None:  # noqa: N802
            # Consume the request body before replying so a 401 on a reused
            # keep-alive connection cannot desynchronize the stream.
            content_length = self._content_length()
            if content_length < 0 or content_length > 65_536:
                # An oversized body is not drained; close to keep the stream clean.
                self.close_connection = True
                self._send_json(413, {"error": "command payload too large"})
                return
            raw_body = self.rfile.read(content_length)
            if not self._authorized():
                return
            try:
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

        def _send_asset(self, content_type: str, asset: str) -> None:
            """Serve the non-secret loopback shell with a no-egress CSP."""
            body = asset.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; connect-src 'self'; script-src 'self'; "
                "style-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _content_length(self) -> int:
            raw_value = self.headers.get("Content-Length")
            if raw_value is None:
                return 0
            try:
                return int(raw_value)
            except ValueError:
                return -1

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

        @staticmethod
        def _system_frame(event: dict[str, Any]) -> bytes:
            data = json.dumps(event, ensure_ascii=False, sort_keys=True)
            offset = event.get("stream_offset")
            prefix = f"id: {offset}\n" if offset is not None else ""
            return (prefix + f"event: system-event\ndata: {data}\n\n").encode("utf-8")

        def _send_system_stream(self, query: dict[str, list[str]]) -> None:
            try:
                raw_offset = query.get("after", [self.headers.get("Last-Event-ID")])[0]
                offset = None if raw_offset is None or raw_offset == "" else int(raw_offset)
                if offset is not None and offset < 0:
                    raise ValueError("stream offset must be nonnegative")
                events = api.system_events(after_stream_offset=offset)
            except ValueError:
                self._send_json(400, {"error": "invalid stream offset"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            follow = query.get("follow", ["0"])[0].lower() in {"1", "true", "yes"}
            self.send_header("Connection", "keep-alive" if follow else "close")
            self.end_headers()
            current = offset or 0
            try:
                for event in events:
                    self.wfile.write(self._system_frame(event))
                    event_offset = cast(int, event["stream_offset"])
                    current = max(current, event_offset)
                self.wfile.flush()
                while follow:
                    time.sleep(0.5)
                    tail = api.system_events(after_stream_offset=current)
                    if not tail:
                        self.wfile.write(b": keep-alive\n\n")
                    for event in tail:
                        self.wfile.write(self._system_frame(event))
                        current = cast(int, event["stream_offset"])
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
    resolution: DaemonCommandResolutionService | None = None,
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
            resolution=resolution,
        ),
    )
    return server


__all__ = ["ControlCommandRouter", "ControlResourceRouter", "make_handler", "serve_local_control"]
