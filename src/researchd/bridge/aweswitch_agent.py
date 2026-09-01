"""Managed Agent protocol bridge for one trusted aweswitch profile.

The bridge is the Agent service. aweswitch remains a profile/environment
launcher and never becomes an Agent identity or a source of controller
authority. Only the existing ``research-agent-json-v1`` request and response
models cross this boundary.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import tempfile
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from researchd.agents.schemas import PlanProposal
from researchd.collaboration.heterogeneous import (
    ManagedAgentTurnRequest,
    ManagedAgentTurnResponse,
)
from researchd.domain.enums import DelegationPurpose
from researchd.domain.review import ReviewDecision

_ENV_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_MANAGED_PROMPT = (
    "Process the managed request context supplied on stdin and return only "
    "the JSON response required by that context."
)
_SAFE_ENVIRONMENT = frozenset({
    "HOME",
    "LANG",
    "LOGNAME",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
    "USER",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "NODE_EXTRA_CA_CERTS",
})


class AweswitchProfileError(ValueError):
    """The selected external profile cannot safely serve managed turns."""


def default_aweswitch_config(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get("AWESWITCH_CONFIG")
    return Path(configured).expanduser() if configured else Path.home() / ".config/aweswitch/config.json"


def load_profile_metadata(
    config_path: Path,
    profile_name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, object]]:
    """Resolve profile metadata without expanding or returning secret values."""
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AweswitchProfileError("aweswitch config is unavailable or invalid") from error
    profiles = document.get("profiles") if isinstance(document, dict) else None
    if not isinstance(profiles, dict):
        raise AweswitchProfileError("aweswitch config has no profiles object")
    matches: list[tuple[str, dict[str, object]]] = []
    for provider, entries in profiles.items():
        if not isinstance(provider, str) or not isinstance(entries, dict):
            continue
        profile = entries.get(profile_name)
        if isinstance(profile, dict):
            matches.append((provider, profile))
    if len(matches) != 1:
        wording = "unknown" if not matches else "ambiguous"
        raise AweswitchProfileError(f"{wording} aweswitch profile: {profile_name}")
    provider, profile = matches[0]
    if provider != "qwen":
        raise AweswitchProfileError(
            f"Developer Preview bridge does not yet support provider: {provider}"
        )
    env = os.environ if environ is None else environ
    profile_env = profile.get("env", {})
    if not isinstance(profile_env, dict):
        raise AweswitchProfileError("aweswitch profile env must be an object")
    missing = sorted({
        match.group(1)
        for value in profile_env.values()
        if isinstance(value, str)
        for match in [_ENV_REFERENCE.fullmatch(value)]
        if match is not None and match.group(1) not in env
    })
    if missing:
        raise AweswitchProfileError(
            "aweswitch profile references missing environment variables: "
            + ", ".join(missing)
        )
    return provider, {"profile": profile_name, "provider": provider}


def build_aweswitch_environment(
    config_path: Path,
    profile_name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the minimal environment allowed to reach the external launcher."""
    env = os.environ if environ is None else environ
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AweswitchProfileError("aweswitch config is unavailable or invalid") from error
    profiles = document.get("profiles") if isinstance(document, dict) else None
    references: set[str] = set()
    if isinstance(profiles, dict):
        for entries in profiles.values():
            if not isinstance(entries, dict):
                continue
            profile = entries.get(profile_name)
            if not isinstance(profile, dict):
                continue
            profile_env = profile.get("env", {})
            if isinstance(profile_env, dict):
                for value in profile_env.values():
                    match = _ENV_REFERENCE.fullmatch(value) if isinstance(value, str) else None
                    if match is not None:
                        references.add(match.group(1))
    allowed = _SAFE_ENVIRONMENT | references
    result = {name: value for name, value in env.items() if name in allowed}
    result["AWESWITCH_CONFIG"] = str(config_path)
    return result


def build_managed_prompt(turn: ManagedAgentTurnRequest) -> str:
    """Build a bounded schema-directed prompt for one canonical turn."""
    request = json.dumps(
        turn.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    schema = json.dumps(
        ManagedAgentTurnResponse.model_json_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    target_schema: dict[str, object] | None = None
    if turn.purpose is DelegationPurpose.PLAN:
        target_schema = PlanProposal.model_json_schema()
    elif turn.purpose is DelegationPurpose.REVIEW:
        target_schema = ReviewDecision.model_json_schema()
    target = json.dumps(
        target_schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        "You are a managed research Agent. Return exactly one JSON object and no "
        "Markdown or commentary. The object must validate against the supplied "
        "ManagedAgentTurnResponse schema. Never claim controller, approval, policy, "
        "verification, or capability authority. For EXECUTE, request only capabilities "
        "listed in payload.granted_capabilities; use execution.final_claim only when "
        "the work is complete. For PLAN or REVIEW, place the typed domain result in "
        "output.\nREQUEST=" + request + "\nRESPONSE_SCHEMA=" + schema
        + "\nTARGET_OUTPUT_SCHEMA=" + target
    )


class AweswitchManagedBridge:
    """Execute one bounded, non-interactive aweswitch turn and validate its result."""

    def __init__(
        self,
        *,
        aweswitch: Path,
        qwen: Path,
        config_path: Path,
        profile: str,
        cwd: Path,
        timeout_seconds: float = 180.0,
        max_output_bytes: int = 1_000_000,
    ) -> None:
        if not aweswitch.is_absolute() or not aweswitch.is_file():
            raise AweswitchProfileError("aweswitch executable must be an absolute file")
        if not qwen.is_absolute() or not qwen.is_file() or not os.access(qwen, os.X_OK):
            raise AweswitchProfileError("qwen executable must be an absolute executable file")
        if not cwd.is_absolute() or not cwd.is_dir():
            raise AweswitchProfileError("bridge cwd must be an absolute directory")
        if timeout_seconds <= 0 or max_output_bytes <= 0:
            raise AweswitchProfileError("bridge bounds must be positive")
        self.aweswitch = aweswitch
        self.qwen = qwen
        self.config_path = config_path
        self.profile = profile
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

    def health(self) -> dict[str, object]:
        try:
            provider, metadata = load_profile_metadata(self.config_path, self.profile)
        except AweswitchProfileError as error:
            return {"healthy": False, "reason": str(error)}
        return {"healthy": True, "provider": provider, **metadata}

    def invoke(self, turn: ManagedAgentTurnRequest) -> ManagedAgentTurnResponse:
        health = self.health()
        if health.get("healthy") is not True:
            raise AweswitchProfileError(str(health.get("reason", "bridge is unhealthy")))
        prompt = build_managed_prompt(turn).encode("utf-8")
        if len(prompt) > 512_000:
            raise AweswitchProfileError("managed turn prompt exceeds bridge input limit")
        command = (
            str(self.aweswitch),
            self.profile,
            "-p",
            _MANAGED_PROMPT,
            "-o",
            "json",
        )
        child_env = build_aweswitch_environment(self.config_path, self.profile)
        # aweswitch launches the provider by its stable command name.  Admit
        # only the directory containing the installer-resolved Qwen binary;
        # never inherit an arbitrary caller PATH into the managed process.
        child_env["PATH"] = os.pathsep.join((str(self.qwen.parent), os.defpath))
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(
                command,
                cwd=self.cwd,
                env=child_env,
                stdin=subprocess.PIPE,
                stdout=stdout,
                stderr=stderr,
                start_new_session=(os.name == "posix"),
            )
            try:
                process.communicate(prompt, timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired as error:
                self._terminate(process)
                raise AweswitchProfileError("aweswitch turn timed out") from error
            if process.returncode != 0:
                raise AweswitchProfileError(
                    f"aweswitch turn failed with exit code {process.returncode}"
                )
            size = stdout.tell()
            if size <= 0 or size > self.max_output_bytes:
                raise AweswitchProfileError("aweswitch output is empty or exceeds limit")
            stdout.seek(0)
            payload = stdout.read(self.max_output_bytes)
        return self._decode(payload)

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.returncode is not None:
            return
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait()

    @staticmethod
    def _decode(payload: bytes) -> ManagedAgentTurnResponse:
        try:
            document: object = json.loads(payload)
            if not isinstance(document, list):
                raise AweswitchProfileError("aweswitch JSON output must be an event array")
            terminal = [
                event
                for event in document
                if isinstance(event, dict) and event.get("type") == "result"
            ]
            if len(terminal) != 1:
                raise AweswitchProfileError(
                    "aweswitch output must contain exactly one terminal result event"
                )
            result = terminal[0]
            if (
                result.get("subtype") != "success"
                or result.get("is_error") is not False
            ):
                raise AweswitchProfileError("aweswitch terminal result is not successful")
            denials = result.get("permission_denials", [])
            if not isinstance(denials, list) or denials:
                raise AweswitchProfileError("aweswitch turn contains permission denials")
            managed = result.get("result")
            if not isinstance(managed, str):
                raise AweswitchProfileError("aweswitch terminal result has no text result")
            return ManagedAgentTurnResponse.model_validate_json(managed)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as error:
            raise AweswitchProfileError("aweswitch returned invalid managed JSON") from error


class _BridgeServer(ThreadingHTTPServer):
    bridge: AweswitchManagedBridge


class AweswitchAgentHandler(BaseHTTPRequestHandler):
    """Loopback HTTP surface for the managed Agent protocol."""

    server_version = "research-aweswitch-agent/1"

    @property
    def bridge(self) -> AweswitchManagedBridge:
        return self.server.bridge  # type: ignore[attr-defined,no-any-return]

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        health = self.bridge.health()
        self._json(200 if health.get("healthy") is True else 503, health)

    def do_POST(self) -> None:
        if self.path != "/invoke":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 256_000:
                raise ValueError("invalid request size")
            turn = ManagedAgentTurnRequest.model_validate_json(self.rfile.read(length))
            response = self.bridge.invoke(turn)
        except (ValueError, ValidationError, AweswitchProfileError) as error:
            self._json(422, {"error": type(error).__name__})
            return
        self._json(200, response.model_dump(mode="json"))

    def _json(self, status: int, payload: Mapping[str, object]) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="research-aweswitch-agent")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--aweswitch", type=Path, required=True)
    parser.add_argument("--qwen", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)
    if args.host not in _LOOPBACK:
        parser.error("aweswitch bridge must bind to loopback")
    config = args.config or default_aweswitch_config()
    bridge = AweswitchManagedBridge(
        aweswitch=args.aweswitch.resolve(strict=True),
        qwen=args.qwen.absolute(),
        config_path=config.resolve(strict=True),
        profile=args.profile,
        cwd=args.cwd.resolve(strict=True),
        timeout_seconds=args.timeout,
    )
    server = _BridgeServer((args.host, args.port), AweswitchAgentHandler)
    server.bridge = bridge
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


__all__ = [
    "AweswitchAgentHandler",
    "AweswitchManagedBridge",
    "AweswitchProfileError",
    "build_managed_prompt",
    "build_aweswitch_environment",
    "default_aweswitch_config",
    "entrypoint",
    "load_profile_metadata",
    "main",
]
