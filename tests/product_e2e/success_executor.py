"""Deterministic success executor for the AC43 failure E2E.

Always responds with a successful execution result. No provider dependency.
"""

import argparse
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class SuccessExecutorHandler(BaseHTTPRequestHandler):
    server_version = "research-success-executor/1"

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

        turn_payload = body.get("payload")
        if not isinstance(turn_payload, dict):
            self._json(422, {"error": "missing turn payload"})
            return
        prior_results = turn_payload.get("prior_results")
        granted = turn_payload.get("granted_capabilities")
        if not isinstance(prior_results, list) or not isinstance(granted, list):
            self._json(422, {"error": "invalid execution payload"})
            return

        if not prior_results and "sandbox.shell" in granted:
            seed = f"{body.get('invocation_id')}:{body.get('attempt_id')}:success"
            request_id = f"step_{hashlib.sha256(seed.encode()).hexdigest()[:24]}"
            execution: dict[str, Any] = {
                "actions": [{
                    "request_id": request_id,
                    "capability": "sandbox.shell",
                    "parameters": {
                        "argv": ["/usr/bin/printf", "replacement Agent completed\n"],
                    },
                }],
                "final_claim": None,
            }
        elif prior_results:
            execution = {
                "actions": [],
                "final_claim": "deterministic replacement Agent completed",
            }
        else:
            self._json(422, {"error": "sandbox.shell was not granted"})
            return

        response: dict[str, Any] = {
            "execution": execution,
        }
        self._json(200, response)

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
    parser = argparse.ArgumentParser(prog="success-executor")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19024)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), SuccessExecutorHandler)
    print(f"success executor listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
