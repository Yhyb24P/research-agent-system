"""Reference reviewer Agent for the PH07 product E2E.

Speaks the managed turn protocol over loopback HTTP, mirroring the shipped
``research-coder-agent`` pilot.  It accepts REVIEW turns only and returns a
schema-valid ``ReviewDecision`` that cites the controller's latest
verification result — the reviewer never invents evidence or verifies its
own inputs.
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from researchd.collaboration.heterogeneous import ManagedAgentTurnRequest
from researchd.domain.enums import DelegationPurpose


class ReviewerHandler(BaseHTTPRequestHandler):
    server_version = "research-reviewer-agent/1"

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
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            turn = ManagedAgentTurnRequest.model_validate(payload)
            if turn.purpose is not DelegationPurpose.REVIEW:
                raise ValueError("pilot reviewer only accepts REVIEW turns")
            context = turn.payload
            work_order_id = context.get("work_order_id")
            verification_id = context.get("verification_id")
            if not isinstance(work_order_id, str) or not isinstance(verification_id, str):
                raise ValueError("review context lacks work_order_id/verification_id")
        except (ValueError, json.JSONDecodeError, TypeError) as error:
            self._json(422, {"error": type(error).__name__})
            return
        decision = {
            "decision": "ACCEPT",
            "work_order_id": work_order_id,
            "evidence_refs": [verification_id],
            "deficiencies": [],
            "rationale": "trusted verification result is green",
            "requested_next_objective": None,
            "requested_evidence": [],
        }
        self._json(200, {"output": decision})

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research-reviewer-agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19012)
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("managed reviewer must bind to loopback")
    server = ThreadingHTTPServer((args.host, args.port), ReviewerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
