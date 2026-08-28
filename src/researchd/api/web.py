"""Loopback-only HTTP client facade over the in-process LocalControlAPI."""
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from researchd.api.control import LocalControlAPI


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
            if parts == ["api", "artifacts"] and query.get("run", [None])[0]:
                return 200, self.api.artifacts(query["run"][0])
            if len(parts) == 3 and parts[:2] == ["api", "timeline"]:
                return 200, self.api.timeline(parts[2])
            if len(parts) == 3 and parts[:2] == ["api", "delegations"]:
                return 200, self.api.delegation(parts[2])
            if len(parts) == 3 and parts[:2] == ["api", "runs"] and parts[2]:
                return 200, self.api.run_status(parts[2])
            if len(parts) == 3 and parts[:2] == ["api", "events"] and parts[2]:
                return 200, {"events": self.api.events(parts[2])}
        except LookupError:
            return 404, {"error": "not found"}
        return 404, {"error": "unknown resource"}


def make_handler(api: LocalControlAPI) -> type[BaseHTTPRequestHandler]:
    router = ControlResourceRouter(api)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            status, payload = router.get(self.path)
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def serve_local_control(api: LocalControlAPI, *, host: str = "127.0.0.1", port: int = 8788) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("control HTTP server must bind loopback")
    server = ThreadingHTTPServer((host, port), make_handler(api))
    return server
