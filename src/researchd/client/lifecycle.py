"""Lifecycle commands for the daily ``research`` client.

Bootstrap is delegated to the trusted ``researchd`` executable — the
client never initializes or migrates the controller database. The
interactive entry may spawn ``researchd serve`` as a child process,
but a daemon that is not READY is surfaced, never bypassed.
"""

import json
import os
import signal
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlencode

import httpx

from researchd.client.shell import run_shell
from researchd.client.transport import (
    ResearchClient,
    TransportError,
    load_owner_token,
)
from researchd.daemon.composition import DaemonConfig
from researchd.daemon.security import ControlCredentialError
from researchd.daemon.identity import identity_path, is_live


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
    """Format an allowed loopback bind address as an HTTP authority."""
    host = f"[{config.host}]" if ":" in config.host else config.host
    return f"http://{host}:{config.port}"


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
            start_new_session=(os.name == "posix"),
        )
    finally:
        log_file.close()


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


def stop_daemon(config_path: Path, *, timeout: float = 10.0) -> int:
    """Stop only the daemon whose persisted strong identity still matches."""
    config = load_client_config(config_path)
    try:
        identity = json.loads(identity_path(config.state_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 1
    if not isinstance(identity, dict) or not is_live(identity):
        return 1
    pid = identity.get("pid")
    if not isinstance(pid, int):
        return 1
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe_health(config) is None:
            return 0
        time.sleep(0.1)
    return 1


def open_browser(
    config_path: Path,
    *,
    open_url: Callable[[str], bool] = webbrowser.open_new_tab,
    print_fn: Callable[[str], None] = print,
) -> int:
    """Open the non-authoritative browser projection for a READY daemon."""
    config = load_client_config(config_path)
    health = probe_health(config)
    if health is None:
        spawn_daemon(config, config_path)
    if health is None or health.get("ready") is not True:
        try:
            wait_for_ready(config)
        except (DaemonNotReadyError, TimeoutError) as error:
            print_fn(f"researchd is not ready: {error}")
            return 1
    try:
        token = load_owner_token(config.state_root)
    except ControlCredentialError as error:
        print_fn(f"cannot load the control credential: {error}")
        return 1
    url = f"{base_url_for(config)}/ui#{urlencode({'token': token})}"
    if open_url(url):
        print_fn("opened the local Browser Control Tower")
    else:
        print_fn(
            f"browser open failed; local Control Tower is available at {base_url_for(config)}/ui; "
            "rerun `research browser` after fixing browser integration",
        )
    return 0


def interactive_entry(
    config_path: Path,
    *,
    spawn: bool = True,
    timeout: float = 30.0,
    input_fn: Callable[[], str] = input,
    print_fn: Callable[[str], None] = print,
) -> int:
    """Reach a READY daemon (spawning one when needed) and enter the shell.

    A spawned or pre-existing daemon remains independent of the client
    window. A daemon that is not READY is never bypassed: the shell is
    only entered after the health probe reports READY.
    """
    config = load_client_config(config_path)
    health = probe_health(config)
    if health is None:
        if not spawn:
            print_fn(
                f"no researchd reachable at {base_url_for(config)}; start one first"
            )
            return 1
        # The daily client may help launch the controller, but it never owns
        # the controller lifecycle. Closing this window must not stop Agent
        # runtimes or alter any authoritative state.
        spawn_daemon(config, config_path)
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
        token = load_owner_token(config.state_root)
    except ControlCredentialError as error:
        print_fn(f"cannot load the control credential: {error}")
        return 1
    client = ResearchClient(base_url_for(config), token)
    try:
        print_fn("research interactive shell; type quit to exit")
        run_shell(client, input_fn=input_fn, print_fn=print_fn)
    finally:
        client.close()
    return 0


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
    "open_browser",
    "probe_health",
    "researchd_argv",
    "run_init",
    "run_status",
    "stop_daemon",
    "spawn_daemon",
    "wait_for_ready",
]
