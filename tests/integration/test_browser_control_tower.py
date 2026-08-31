"""PX09: Browser Control Tower deferred test matrix.

Covers the frozen 推进计划.md matrix:
① /ui assets readable without a token while every /api/* read and mutation
   route still rejects anonymous traffic;
② asset responses carry the no-egress CSP and cache/referrer/nosniff headers;
③ ``research browser`` keeps the credential in the URL fragment only, prints
   no token on success, prints a usable hint URL on opener failure, and
   brackets the IPv6 loopback host;
④ the tower script only calls existing endpoints and its payloads cannot
   inject actor/endpoint/tenant/launch material — routes fail closed with a
   pinned HUMAN actor and typed validation;
⑤ refresh, command receipt replay, and browser close leave the daemon,
   leases, and authoritative tables untouched.
"""

import json
import os
import re
import socket
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from researchd.api.browser_assets import BROWSER_ASSETS, BROWSER_INDEX, BROWSER_JS
from researchd.api.control import LocalControlAPI
from researchd.api.web import make_handler
from researchd.client.lifecycle import base_url_for, open_browser
from researchd.daemon.command_service import DurableDaemonCommandService
from researchd.daemon.composition import DaemonConfig
from researchd.daemon.contracts import (
    DaemonCommand,
    DaemonCommandResult,
    RemoteAgentAttachCommand,
)
from researchd.daemon.runtime import ResearchDaemon
from researchd.daemon.startup import StartupBarrier, StartupPhase
from researchd.domain.base import DomainModel
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import AuditEventRecord, DaemonCommandRecord
from tests.integration.test_storage import migrate

TOKEN = "f" * 64


class _RecordingDispatcher:
    """Accepts any typed command and records the dispatched identity."""

    def __init__(self) -> None:
        self.commands: list[DomainModel] = []

    def __call__(self, command: DomainModel) -> DaemonCommandResult:
        assert isinstance(command, DaemonCommand)
        self.commands.append(command)
        return DaemonCommandResult(
            command_id=command.command_id,
            command_type=type(command).__name__.removesuffix("Command"),
            status="ACCEPTED",
            resource={"accepted": True},
        )


class _IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def _server(
    tmp_path: Path,
    host: str = "127.0.0.1",
) -> tuple[ThreadingHTTPServer, int, _RecordingDispatcher, ResearchDaemon, sessionmaker[Session]]:
    database = tmp_path / "browser_tower.db"
    migrate(database)
    sessions: sessionmaker[Session] = session_factory(create_sqlite_engine(database))
    api = LocalControlAPI(sessions)
    dispatcher = _RecordingDispatcher()
    durable = DurableDaemonCommandService(sessions, dispatcher)
    barrier = StartupBarrier({phase: lambda: None for phase in StartupPhase})
    daemon = ResearchDaemon(barrier, durable)
    assert daemon.start().ready is True
    handler = make_handler(api, daemon, control_token=TOKEN)
    if host == "::1":
        server: ThreadingHTTPServer = _IPv6ThreadingHTTPServer((host, 0), handler)
    else:
        server = ThreadingHTTPServer((host, 0), handler)
    port = int(server.server_address[1])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port, dispatcher, daemon, sessions


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _browser_config(tmp_path: Path, port: int, host: str = "127.0.0.1") -> Path:
    config = tmp_path / "researchd.json"
    config.write_text(
        json.dumps({
            "database": str(tmp_path / "browser_tower.db"),
            "artifact_root": str(tmp_path / "artifacts"),
            "state_root": str(tmp_path / "state"),
            "repositories": {},
            "job_commands": {},
            "host": host,
            "port": port,
        }),
        encoding="utf-8",
    )
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    token = state_root / "control.token"
    token.write_text(f"{TOKEN}\n", encoding="ascii")
    os.chmod(token, 0o600)
    return config


def _row_counts(sessions: sessionmaker[Session]) -> tuple[int, int]:
    with sessions() as session:
        commands = session.scalar(select(func.count()).select_from(DaemonCommandRecord))
        events = session.scalar(select(func.count()).select_from(AuditEventRecord))
    assert commands is not None and events is not None
    return commands, events


# Matrix ① — assets open, API closed.

def test_ui_assets_readable_without_token_and_leak_no_credentials(tmp_path: Path) -> None:
    server, port, _, _daemon, _sessions = _server(tmp_path)
    try:
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(timeout=10) as client:
            for path, (content_type, asset) in BROWSER_ASSETS.items():
                response = client.get(f"{base}{path}")
                assert response.status_code == 200
                assert response.headers["Content-Type"] == content_type
                assert response.text == asset
                assert TOKEN not in response.text
            for path in (
                "/api/runs",
                "/api/agents",
                "/api/approvals",
                "/api/handoffs",
                "/api/system-stream",
            ):
                response = client.get(f"{base}{path}")
                assert response.status_code == 401
                assert response.headers.get("WWW-Authenticate") == "Bearer"
            response = client.post(
                f"{base}/api/remote-agents/attach",
                json={"command_id": "cmd_anonymous", "runtime_id": "runtime_ab1"},
            )
            assert response.status_code == 401
            health = client.get(f"{base}/api/health")
            assert health.status_code == 200
            assert health.json()["ready"] is True
            for path in ("/api/runs", "/api/agents", "/api/approvals", "/api/handoffs"):
                assert client.get(f"{base}{path}", headers=_bearer(TOKEN)).status_code == 200
    finally:
        server.shutdown()
        server.server_close()


def test_anonymous_post_body_cannot_desynchronize_a_reused_connection(
    tmp_path: Path,
) -> None:
    """A rejected POST must consume its body before the next HTTP/1.1 request."""
    server, port, _dispatcher, _daemon, _sessions = _server(tmp_path)
    try:
        body = json.dumps({
            "command_id": "cmd_anonymous_keepalive",
            "runtime_id": "runtime_ab1",
        }).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request(
                "POST",
                "/api/remote-agents/attach",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            rejected = connection.getresponse()
            assert rejected.status == 401
            rejected.read()
            connection.request("GET", "/api/health")
            health = connection.getresponse()
            assert health.status == 200
            assert json.loads(health.read())["ready"] is True
        finally:
            connection.close()
    finally:
        server.shutdown()
        server.server_close()


# Matrix ② — no-egress response posture.

def test_asset_responses_carry_no_egress_security_headers(tmp_path: Path) -> None:
    server, port, _, _daemon, _sessions = _server(tmp_path)
    try:
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(timeout=10) as client:
            for path in ("/ui", "/ui/app.css", "/ui/app.js"):
                response = client.get(f"{base}{path}")
                assert response.status_code == 200
                assert response.headers["Cache-Control"] == "no-store"
                assert response.headers["Referrer-Policy"] == "no-referrer"
                assert response.headers["X-Content-Type-Options"] == "nosniff"
                csp = response.headers["Content-Security-Policy"]
                assert "default-src 'none'" in csp
                assert "connect-src 'self'" in csp
                assert "script-src 'self'" in csp
                assert "style-src 'self'" in csp
                assert "frame-ancestors 'none'" in csp
                assert "*" not in csp
                assert "https:" not in csp
    finally:
        server.shutdown()
        server.server_close()


def test_browser_assets_reference_no_external_origin() -> None:
    for _path, (_content_type, asset) in BROWSER_ASSETS.items():
        assert "http://" not in asset
        assert "https://" not in asset
    references = re.findall(r'(?:src|href)="([^"]+)"', BROWSER_INDEX)
    assert set(references) == {"/ui/app.css", "/ui/app.js"}


# Matrix ③ — research browser credential handling.

def test_open_browser_keeps_token_in_fragment_only_and_prints_no_secret(
    tmp_path: Path,
) -> None:
    server, port, _, _daemon, _sessions = _server(tmp_path)
    try:
        config = _browser_config(tmp_path, port)
        opened: list[str] = []

        def _capture(url: str) -> bool:
            opened.append(url)
            return True

        printed: list[str] = []
        exit_code = open_browser(
            config, open_url=_capture, print_fn=printed.append,
        )
        assert exit_code == 0
        assert len(opened) == 1
        split = urlsplit(opened[0])
        assert split.scheme == "http"
        assert split.hostname == "127.0.0.1"
        assert split.port == port
        assert split.path == "/ui"
        assert parse_qs(split.fragment) == {"token": [TOKEN]}
        assert "token" not in split.query
        with httpx.Client(timeout=10) as client:
            assert client.get(f"http://127.0.0.1:{port}/ui").status_code == 200
        assert printed == ["opened the local Browser Control Tower"]
        assert all(TOKEN not in line for line in printed)
    finally:
        server.shutdown()
        server.server_close()


def test_open_browser_opener_failure_redacts_the_credential(tmp_path: Path) -> None:
    server, port, _, _daemon, _sessions = _server(tmp_path)
    try:
        config = _browser_config(tmp_path, port)
        printed: list[str] = []
        exit_code = open_browser(config, open_url=lambda url: False, print_fn=printed.append)
        assert exit_code == 0
        assert len(printed) == 1
        line = printed[0]
        assert line.startswith("browser open failed; local Control Tower is available at ")
        # PH04: the fallback hint must not leak the credential or the
        # fragment URL; only the fragment-free base URL is printable.
        assert TOKEN not in line
        assert "#" not in line
        url = line.split("available at ", 1)[1].split(";", 1)[0]
        split = urlsplit(url)
        assert split.scheme == "http"
        assert split.netloc == f"127.0.0.1:{port}"
        assert split.path == "/ui"
        assert split.fragment == ""
        with httpx.Client(timeout=10) as client:
            assert client.get(url).status_code == 200
    finally:
        server.shutdown()
        server.server_close()


def test_open_browser_brackets_ipv6_loopback_host(tmp_path: Path) -> None:
    server, port, _, _daemon, _sessions = _server(tmp_path, host="::1")
    try:
        config = _browser_config(tmp_path, port, host="::1")
        printed: list[str] = []
        exit_code = open_browser(config, open_url=lambda url: False, print_fn=printed.append)
        assert exit_code == 0
        line = printed[0]
        assert line.startswith("browser open failed; local Control Tower is available at ")
        # PH04: the IPv6 hint keeps the bracketed host and drops the fragment.
        assert TOKEN not in line
        assert "#" not in line
        url = line.split("available at ", 1)[1].split(";", 1)[0]
        split = urlsplit(url)
        assert split.netloc == f"[::1]:{port}"
        assert split.path == "/ui"
        assert split.fragment == ""
        with httpx.Client(timeout=10) as client:
            assert client.get(url).status_code == 200
    finally:
        server.shutdown()
        server.server_close()


def test_base_url_for_brackets_ipv6_host() -> None:
    ipv6 = DaemonConfig(
        database=Path("/tmp/browser-tower/researchd.db"),
        artifact_root=Path("/tmp/browser-tower/artifacts"),
        state_root=Path("/tmp/browser-tower/state"),
        host="::1",
        port=8788,
    )
    assert base_url_for(ipv6) == "http://[::1]:8788"
    ipv4 = DaemonConfig(
        database=Path("/tmp/browser-tower/researchd.db"),
        artifact_root=Path("/tmp/browser-tower/artifacts"),
        state_root=Path("/tmp/browser-tower/state"),
        host="127.0.0.1",
        port=8788,
    )
    assert base_url_for(ipv4) == "http://127.0.0.1:8788"


# Matrix ④ — script endpoint discipline and fail-closed routes.

_ALLOWED_BROWSER_ENDPOINTS = {
    "/api/runs",
    "/api/agents",
    "/api/approvals",
    "/api/handoffs",
    "/api/runs/{id}/messages",
    "/api/agents/{id}/console",
    "/api/system-stream",
    "/api/runs/{id}/cancel",
    "/api/remote-agents/attach",
    "/api/remote-agents/renew",
    "/api/remote-agents/detach",
}


def _extract_js_api_paths(js: str) -> set[str]:
    paths = set(re.findall(r"'(/api/[^']+)'", js))
    for template in re.findall(r"`(/api/[^`]*)`", js):
        normalized = re.sub(r"\$\{encodeURIComponent\([^)]*\)\}", "{id}", template)
        normalized = re.sub(r"\$\{[^}]*\}", "", normalized)
        normalized = normalized.split("?")[0]
        if normalized.endswith("/api/remote-agents/"):
            paths.update(f"{normalized}{verb}" for verb in ("attach", "renew", "detach"))
        else:
            paths.add(normalized)
    return paths


def test_browser_script_only_calls_existing_endpoints() -> None:
    assert _extract_js_api_paths(BROWSER_JS) <= _ALLOWED_BROWSER_ENDPOINTS


def test_browser_command_payloads_carry_no_injected_identity() -> None:
    assert "command_id: commandId()" in BROWSER_JS
    assert "runtime_id: runtimeId" in BROWSER_JS
    lowered = BROWSER_JS.lower()
    for forbidden in ("actor", "tenant", "launch", "endpoint", "spec"):
        assert forbidden not in lowered


def test_cancel_route_rejects_injected_identity(tmp_path: Path) -> None:
    server, port, dispatcher, _daemon, _sessions = _server(tmp_path)
    try:
        payload: dict[str, Any] = {
            "command_id": "cmd_inject_cancel",
            "actor": "evil",
            "actor_type": "SYSTEM",
            "tenant": "tenant_x",
            "endpoint": "http://attacker.example",
            "launch_spec": {"command": ["rm", "-rf", "/"]},
        }
        response = httpx.post(
            f"http://127.0.0.1:{port}/api/runs/run_inject/cancel",
            json=payload,
            headers=_bearer(TOKEN),
            timeout=10,
        )
        assert response.status_code == 422
        assert response.json()["error"] == "invalid command"
        assert dispatcher.commands == []
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("verb", ["attach", "renew", "detach"])
def test_remote_agent_routes_reject_injected_identity(
    tmp_path: Path,
    verb: str,
) -> None:
    server, port, dispatcher, _daemon, _sessions = _server(tmp_path)
    try:
        payload: dict[str, Any] = {
            "command_id": f"cmd_inject_{verb}",
            "runtime_id": "runtime_ab1",
            "actor": "evil",
            "actor_type": "SYSTEM",
            "tenant": "tenant_x",
            "endpoint": "http://attacker.example",
            "launch_spec": {"command": ["rm", "-rf", "/"]},
        }
        response = httpx.post(
            f"http://127.0.0.1:{port}/api/remote-agents/{verb}",
            json=payload,
            headers=_bearer(TOKEN),
            timeout=10,
        )
        assert response.status_code == 422
        assert response.json()["error"] == "invalid command"
        assert dispatcher.commands == []
    finally:
        server.shutdown()
        server.server_close()


def test_remote_agent_routes_require_typed_runtime_id(tmp_path: Path) -> None:
    server, port, dispatcher, _daemon, _sessions = _server(tmp_path)
    try:
        response = httpx.post(
            f"http://127.0.0.1:{port}/api/remote-agents/attach",
            json={"command_id": "cmd_bad_runtime", "runtime_id": "nope"},
            headers=_bearer(TOKEN),
            timeout=10,
        )
        assert response.status_code == 422
        assert dispatcher.commands == []
    finally:
        server.shutdown()
        server.server_close()


def test_routes_pin_human_actor_from_the_route_not_the_payload(tmp_path: Path) -> None:
    server, port, dispatcher, _daemon, _sessions = _server(tmp_path)
    try:
        response = httpx.post(
            f"http://127.0.0.1:{port}/api/remote-agents/attach",
            json={"command_id": "cmd_pinned", "runtime_id": "runtime_ab1"},
            headers=_bearer(TOKEN),
            timeout=10,
        )
        assert response.status_code == 202
        assert len(dispatcher.commands) == 1
        command = dispatcher.commands[0]
        assert isinstance(command, RemoteAgentAttachCommand)
        assert command.actor_type == "HUMAN"
        assert command.actor_id == "local-control-client"
        assert command.runtime_id == "runtime_ab1"
    finally:
        server.shutdown()
        server.server_close()


# Matrix ⑤ — browser lifecycle has no authoritative effect.

def test_refresh_receipt_replay_and_browser_close_leave_authoritative_state_intact(
    tmp_path: Path,
) -> None:
    server, port, dispatcher, daemon, sessions = _server(tmp_path)
    try:
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(timeout=10) as client:
            for path in ("/api/runs", "/api/agents", "/api/approvals", "/api/handoffs"):
                assert client.get(f"{base}{path}", headers=_bearer(TOKEN)).status_code == 200
            assert dispatcher.commands == []
            refreshed = _row_counts(sessions)
            assert _row_counts(sessions) == refreshed

            payload = {"command_id": "cmd_replay", "runtime_id": "runtime_ab1"}
            first = client.post(
                f"{base}/api/remote-agents/attach",
                json=payload,
                headers=_bearer(TOKEN),
            )
            assert first.status_code == 202
            assert len(dispatcher.commands) == 1
            after_first = _row_counts(sessions)
            assert (after_first[0] - refreshed[0], after_first[1] - refreshed[1]) == (1, 2)

            replay = client.post(
                f"{base}/api/remote-agents/attach",
                json=payload,
                headers=_bearer(TOKEN),
            )
            assert replay.status_code == 202
            assert replay.json() == first.json()
            assert len(dispatcher.commands) == 1
            conflict = client.post(
                f"{base}/api/remote-agents/attach",
                json={**payload, "runtime_id": "runtime_other"},
                headers=_bearer(TOKEN),
            )
            assert conflict.status_code == 409
            assert _row_counts(sessions) == after_first

            stream = client.get(f"{base}/api/system-stream", headers=_bearer(TOKEN))
            assert stream.status_code == 200
            assert _row_counts(sessions) == after_first
        # Browser close: the client stops; the daemon and its tables are untouched.
        assert daemon.health()["ready"] is True
        with httpx.Client(timeout=10) as client:
            assert client.get(f"{base}/api/health").status_code == 200
        assert _row_counts(sessions) == after_first
    finally:
        server.shutdown()
        server.server_close()
