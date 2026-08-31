"""Reference planner Agent for the PH07 product E2E.

Speaks the managed turn protocol over loopback HTTP, mirroring the shipped
``research-coder-agent`` pilot.  It accepts PLAN turns only and returns a
fixed, schema-valid ``PlanProposal`` whose single WorkOrder requests the
``sandbox.shell`` capability and accepts on a ``run_log`` artifact — the
same evidence chain the pilot coder produces.  No credentials, no
capability self-grant, no launch overrides.
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from researchd.collaboration.heterogeneous import ManagedAgentTurnRequest
from researchd.domain.enums import DelegationPurpose

#: Fixed plan: one WorkOrder that the pilot coder can execute end to end.
PLAN_PROPOSAL: dict[str, Any] = {
    "proposal_id": "plan_ph07",
    "hypotheses": [],
    "proposed_work_orders": [{
        "proposal_id": "wo_ph07",
        "objective": "produce a trusted execution artifact",
        "inputs": [],
        "requested_capabilities": ["sandbox.shell"],
        "constraints": {"network": "none", "writable_paths": []},
        "budget": {"max_wall_seconds": 60},
        "acceptance": [{
            "criterion_id": "c_run_log",
            "type": "artifact",
            "artifact_type": "run_log",
            "min_count": 1,
        }],
        "expected_outputs": [],
        "data_policy": {"default_classification": "LOCAL_ONLY"},
        "evidence_refs": [],
    }],
    "risks": [],
    "required_evidence": [],
}


class PlannerHandler(BaseHTTPRequestHandler):
    server_version = "research-planner-agent/1"

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
            if turn.purpose is not DelegationPurpose.PLAN:
                raise ValueError("pilot planner only accepts PLAN turns")
        except (ValueError, json.JSONDecodeError) as error:
            self._json(422, {"error": type(error).__name__})
            return
        self._json(200, {"output": PLAN_PROPOSAL})

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
    parser = argparse.ArgumentParser(prog="research-planner-agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19011)
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("managed planner must bind to loopback")
    server = ThreadingHTTPServer((args.host, args.port), PlannerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
