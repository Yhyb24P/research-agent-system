"""Lifecycle commands for the daily ``research`` client.

Bootstrap is delegated to the trusted ``researchd`` executable — the
client never initializes or migrates the controller database. The
interactive entry may spawn ``researchd serve`` as a child process,
but a daemon that is not READY is surfaced, never bypassed.
"""

import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from researchd.client.transport import (
    ResearchClient,
    TransportError,
    load_owner_token,
)
from researchd.daemon.composition import DaemonConfig
from researchd.daemon.security import ControlCredentialError


class DaemonNotReadyError(RuntimeError):
    """The daemon answered health but has not passed its startup barrier."""

    def __init__(self, health: dict[str, object]) -> None:
        self.health = health
        detail = ""
        failed = _failed_phase(health)
        if failed is not None:
            detail = (
                f"; failed phase {failed.get('phase')} ({failed.get('error_type')})"
            )
        super().__init__(
            f"researchd is not READY (state={health.get('state')}){detail}"
        )


def load_client_config(config_path: Path) -> DaemonConfig:
    """Parse the strict JSON daemon config without touching any state."""
    return DaemonConfig.model_validate_json(config_path.read_text(encoding="utf-8"))


def base_url_for(config: DaemonConfig) -> str:
    return f"http://{config.host}:{config.port}"


def researchd_argv(config_path: Path, *subcommand: str) -> list[str]:
    """Trusted-executable argv; the client only ever spawns these forms."""
    return [
        sys.executable,
        "-m",
        "researchd.daemon.cli",
        "--config",
        str(config_path),
        *subcommand,
    ]


def run_init(config_path: Path) -> int:
    """Delegate bootstrap to ``researchd init`` and forward its exit code."""
    completed = subprocess.run(researchd_argv(config_path, "init"))
    return completed.returncode


def probe_health(config: DaemonConfig) -> dict[str, object] | None:
    """Return the health document, or None when no daemon is reachable.

    A non-ready daemon answers 503 with the health document as the body;
    the startup report inside it is how the client surfaces failures.
    """
    client = ResearchClient(base_url_for(config))
    try:
        return client.health()
    except TransportError as error:
        if error.status == 503:
            return error.payload
        return None
    except httpx.HTTPError:
        return None
    finally:
        client.close()


def wait_for_ready(
    config: DaemonConfig,
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.25,
) -> dict[str, object]:
    """Poll health until READY; fail fast on a FAILED startup report."""
    deadline = time.monotonic() + timeout
    last: dict[str, object] | None = None
    while True:
        last = probe_health(config)
        if last is not None:
            if last.get("ready") is True:
                return last
            if last.get("state") == "FAILED":
                raise DaemonNotReadyError(last)
        if time.monotonic() >= deadline:
            if last is not None:
                raise DaemonNotReadyError(last)
            raise TimeoutError("researchd did not become reachable")
        time.sleep(poll_interval)


def spawn_daemon(config: DaemonConfig, config_path: Path) -> subprocess.Popen[bytes]:
    """Start ``researchd serve`` as a child process logging into state_root."""
    log_path = config.state_root / "daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab")
    try:
        return subprocess.Popen(
            researchd_argv(config_path, "serve"),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    finally:
        log_file.close()


def terminate_daemon(process: subprocess.Popen[bytes] | None) -> None:
    """Terminate a spawned daemon without leaving it behind."""
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_status(
    config_path: Path,
    *,
    print_fn: Callable[[str], None] = print,
) -> int:
    """Report reachability and readiness as one JSON document."""
    config = load_client_config(config_path)
    health = probe_health(config)
    if health is None:
        print_fn(json.dumps({"reachable": False}, sort_keys=True))
        return 1
    document = {"reachable": True, **health}
    print_fn(json.dumps(document, sort_keys=True))
    return 0 if health.get("ready") is True else 1


def interactive_entry(
    config_path: Path,
    *,
    spawn: bool = True,
    timeout: float = 30.0,
    input_fn: Callable[[], str] = input,
    print_fn: Callable[[str], None] = print,
) -> int:
    """Reach a READY daemon (spawning one when needed) and enter the shell.

    A spawned daemon is terminated when the shell exits; a pre-existing
    daemon is left running. A daemon that is not READY is never bypassed:
    the shell is only entered after the health probe reports READY.
    """
    config = load_client_config(config_path)
    health = probe_health(config)
    spawned: subprocess.Popen[bytes] | None = None
    try:
        if health is None:
            if not spawn:
                print_fn(
                    f"no researchd reachable at {base_url_for(config)}; "
                    "start one first"
                )
                return 1
            spawned = spawn_daemon(config, config_path)
            try:
                health = wait_for_ready(config, timeout=timeout)
            except (DaemonNotReadyError, TimeoutError) as error:
                print_fn(f"researchd did not become ready: {error}")
                return 1
        elif health.get("ready") is not True:
            try:
                health = wait_for_ready(config, timeout=timeout)
            except (DaemonNotReadyError, TimeoutError) as error:
                print_fn(f"researchd is not ready: {error}")
                return 1
        try:
            load_owner_token(config.state_root)
        except ControlCredentialError as error:
            print_fn(f"cannot load the control credential: {error}")
            return 1
        print_fn("research interactive shell; type quit to exit")
        while True:
            try:
                line = input_fn().strip()
            except EOFError:
                break
            if line in {"quit", "exit"}:
                break
            print_fn(f"unknown command: {line}")
        return 0
    finally:
        terminate_daemon(spawned)


def _failed_phase(health: dict[str, object]) -> dict[str, object] | None:
    startup = health.get("startup")
    if not isinstance(startup, dict):
        return None
    phases = startup.get("phases")
    if not isinstance(phases, list):
        return None
    for phase in phases:
        if isinstance(phase, dict) and phase.get("status") == "FAIL":
            return phase
    return None


__all__ = [
    "DaemonNotReadyError",
    "base_url_for",
    "interactive_entry",
    "load_client_config",
    "probe_health",
    "researchd_argv",
    "run_init",
    "run_status",
    "spawn_daemon",
    "terminate_daemon",
    "wait_for_ready",
]
