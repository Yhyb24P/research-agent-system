"""Deterministic unavailable executor for the AC43 failure E2E."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FailingExecutorHandler(BaseHTTPRequestHandler):
    server_version = "research-failing-executor/1"

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        self._json(200, {"healthy": True})

    def do_POST(self) -> None:
        if self.path != "/invoke":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 256_000:
                self._json(400, {"error": "invalid payload size"})
                return
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self._json(400, {"error": "invalid JSON"})
            return

        del body
        # A real Agent endpoint can become unavailable independently of any
        # model/provider.  Exercise that adapter boundary with a stable 503.
        self._json(503, {"error": "deterministic_agent_unavailable"})

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


def main() -> None:
    parser = argparse.ArgumentParser(prog="failing-executor")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19023)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), FailingExecutorHandler)
    print(f"failing executor listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
