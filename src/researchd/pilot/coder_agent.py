"""Credential-free reference coder Agent for the managed invocation pilot."""

import argparse
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from researchd.collaboration.heterogeneous import ManagedAgentTurnRequest, ManagedAgentTurnResponse
from researchd.domain.enums import Capability
from researchd.executor.contracts import CapabilityRequest, LocalAgentResponse


class PilotCoderHandler(BaseHTTPRequestHandler):
    server_version = "research-coder-agent/1"

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
            response = ManagedAgentTurnResponse(execution=self._complete(turn))
        except (ValueError, json.JSONDecodeError) as error:
            self._json(422, {"error": type(error).__name__})
            return
        self._json(200, response.model_dump(mode="json"))

    @staticmethod
    def _complete(turn: ManagedAgentTurnRequest) -> LocalAgentResponse:
        request = turn.request
        if not request.prior_results and Capability.SANDBOX_SHELL in request.granted_capabilities:
            digest = hashlib.sha256(
                f"{turn.invocation_id}:{turn.attempt_id}:pilot-output".encode()
            ).hexdigest()[:24]
            return LocalAgentResponse(actions=(CapabilityRequest(
                request_id=f"step_{digest}",
                capability=Capability.SANDBOX_SHELL,
                parameters={"argv": ["/usr/bin/printf", "managed coder invocation\n"]},
            ),))
        if not request.prior_results and Capability.GIT_STATUS in request.granted_capabilities:
            digest = hashlib.sha256(
                f"{turn.invocation_id}:{turn.attempt_id}:git-status".encode()
            ).hexdigest()[:24]
            return LocalAgentResponse(actions=(CapabilityRequest(
                request_id=f"step_{digest}", capability=Capability.GIT_STATUS, parameters={},
            ),))
        if request.prior_results:
            latest = request.prior_results[-1]
            outcome = "completed" if latest.status == "ok" else "failed"
            return LocalAgentResponse(final_claim=f"repository inspection {outcome}")
        return LocalAgentResponse(final_claim="no granted pilot action was available")

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
    parser = argparse.ArgumentParser(prog="research-coder-agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19003)
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("managed coder must bind to loopback")
    server = ThreadingHTTPServer((args.host, args.port), PilotCoderHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
