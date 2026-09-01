"""Trusted first-run setup and read-only diagnostics for Developer Preview."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from researchd.client.config_discovery import (
    default_config_path,
    default_data_root,
    default_state_root,
    resolve_config_path,
)
from researchd.client.lifecycle import (
    base_url_for,
    load_client_config,
    probe_health,
    researchd_argv,
    spawn_daemon,
    wait_for_ready,
)
from researchd.client.transport import ResearchClient, load_owner_token
from researchd.daemon.security import control_token_path


@dataclass(frozen=True)
class SetupResult:
    """Result of a successful first-run profile creation."""

    config_path: Path
    project_root: Path


SetupRole = Literal["planner", "coder", "reviewer"]


def discover_git_root(start: Path) -> Path | None:
    """Return the canonical Git worktree root containing ``start``."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    root = Path(completed.stdout.strip())
    return root.resolve() if root.is_absolute() else None


def setup_payload(
    project_root: Path,
    *,
    data_root: Path,
    state_root: Path,
    port: int = 8788,
) -> dict[str, Any]:
    """Build the strict, credential-free daemon config for one local project."""
    project = project_root.resolve(strict=True)
    return {
        "database": str(data_root / "researchd.db"),
        "artifact_root": str(data_root / "artifacts"),
        "state_root": str(state_root),
        "repositories": {"project": str(project)},
        "workspace_sources": {
            "workspace_local": {
                "root": str(project),
                "transport_kind": "GIT_WORKTREE",
                "access_mode": "READ_WRITE",
                "allowed_paths": ["."],
                "classification_ceiling": "LOCAL_ONLY",
            },
        },
        "job_commands": {},
        "workspace_capabilities": ["sandbox.shell"],
        "user_capabilities": ["sandbox.shell"],
        "host": "127.0.0.1",
        "port": port,
    }


def write_new_config(path: Path, payload: Mapping[str, Any]) -> None:
    """Create one owner-only trusted config without replacing an existing file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def run_setup(
    *,
    project: Path | None = None,
    config_path: Path | None = None,
    port: int = 8788,
    assume_yes: bool = False,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    cwd: Path | None = None,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    run_fn: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    initialize_workspace_fn: Callable[[Path], None] | None = None,
    agent_roles: tuple[SetupRole, ...] | None = None,
    profile_ref: str | None = None,
    install_agents_fn: Callable[[Path, tuple[SetupRole, ...], str], None] | None = None,
) -> SetupResult | None:
    """Create and initialize a fresh trusted local installation profile."""
    print_fn("Research Developer Preview")
    env = os.environ if environ is None else environ
    base_home = Path.home() if home is None else home
    start = Path.cwd() if cwd is None else cwd
    target_config = (
        config_path.resolve()
        if config_path is not None
        else default_config_path(env, base_home)
    )
    if target_config.exists():
        print_fn(f"refusing to replace existing research config: {target_config}")
        return None

    detected = project.resolve() if project is not None else discover_git_root(start)
    if detected is None and not assume_yes:
        answer = input_fn("Git project path: ").strip()
        detected = Path(answer).expanduser().resolve() if answer else None
    if detected is None or discover_git_root(detected) != detected:
        print_fn("setup requires the root of an existing Git worktree")
        return None
    if not assume_yes:
        answer = input_fn(f"Use project {detected}? [Y/n] ").strip().lower()
        if answer not in {"", "y", "yes"}:
            print_fn("setup cancelled")
            return None

    data_root = default_data_root(base_home).resolve()
    state_root = default_state_root(base_home).resolve()
    database = data_root / "researchd.db"
    if database.exists() or control_token_path(state_root).exists():
        print_fn(
            "refusing fresh setup because local research state already exists; "
            "restore the matching config or move the state deliberately"
        )
        return None

    payload = setup_payload(
        detected,
        data_root=data_root,
        state_root=state_root,
        port=port,
    )
    write_new_config(target_config, payload)
    print_fn(f"created trusted config: {target_config}")

    for command in ("validate", "init"):
        completed = run_fn(researchd_argv(target_config, command))
        if completed.returncode != 0:
            print_fn(f"researchd {command} failed with exit code {completed.returncode}")
            return None
    selected_roles = agent_roles
    if selected_roles is None and not assume_yes:
        from researchd.client.agent_management import default_profile_ref

        discovered_profile = profile_ref or default_profile_ref()
        if discovered_profile is not None:
            answer = input_fn(
                "Add planner, coder and reviewer using "
                f"{discovered_profile}? [Y/n] "
            ).strip().lower()
            if answer in {"", "y", "yes"}:
                selected_roles = ("planner", "coder", "reviewer")
                profile_ref = discovered_profile
    if selected_roles:
        from researchd.client.agent_management import (
            default_profile_ref,
            install_aweswitch_agents_for_setup,
        )

        selected_profile = profile_ref or default_profile_ref()
        if selected_profile is None:
            print_fn(
                "no unambiguous supported aweswitch profile; "
                "setup will continue without Agents"
            )
        else:
            installer = install_agents_fn or install_aweswitch_agents_for_setup
            try:
                installer(target_config, selected_roles, selected_profile)
                print_fn(
                    "installed Preview Agents: " + ", ".join(selected_roles)
                )
            except (OSError, RuntimeError, ValueError) as error:
                print_fn(
                    "Agent installation was skipped after a safe failure: "
                    f"{type(error).__name__}"
                )
    initializer = (
        initialize_default_workspace
        if initialize_workspace_fn is None
        else initialize_workspace_fn
    )
    try:
        initializer(target_config)
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        print_fn(f"default workspace initialization failed: {type(error).__name__}")
        return None
    print_fn(f"initialized Developer Preview workspace: {detected}")
    return SetupResult(target_config, detected)


def initialize_default_workspace(config_path: Path) -> None:
    """Create the setup workspace through the authenticated control API."""
    config = load_client_config(config_path)
    health = probe_health(config)
    if health is None:
        spawn_daemon(config, config_path)
        health = wait_for_ready(config)
    if health.get("ready") is not True:
        raise RuntimeError("researchd did not become ready")
    client = ResearchClient(base_url_for(config), load_owner_token(config.state_root))
    try:
        workspaces = client.get("/api/workspaces")
        if not any(item.get("workspace_id") == "workspace_local" for item in workspaces):
            client.post_command(
                "/api/workspaces",
                {"workspace_id": "workspace_local", "name": "Local project"},
            )
        for agent in client.get("/api/agents"):
            if agent.get("enabled") is True:
                client.post_command(f"/api/agents/{agent['agent_id']}/start", {})
    finally:
        client.close()


def doctor_report(
    explicit_config: Path | None,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[dict[str, Any], bool]:
    """Return a non-secret diagnostic projection and whether it is usable."""
    config_path = resolve_config_path(explicit_config, environ, home)
    report: dict[str, Any] = {
        "config": str(config_path) if config_path is not None else None,
        "config_present": config_path is not None and config_path.is_file(),
        "tools": {
            name: shutil.which(name) is not None
            for name in ("git", "bwrap", "aweswitch")
        },
    }
    if config_path is None or not config_path.is_file():
        report["usable"] = False
        report["guidance"] = "run `research setup`"
        return report, False
    mode = stat.S_IMODE(config_path.stat().st_mode)
    report["config_owner_only"] = mode & 0o077 == 0
    try:
        config = load_client_config(config_path)
    except (OSError, ValidationError, ValueError) as error:
        report["config_valid"] = False
        report["error_type"] = type(error).__name__
        report["usable"] = False
        return report, False
    report["config_valid"] = True
    report["database_present"] = config.database.is_file()
    token = control_token_path(config.state_root)
    report["control_credential_present"] = token.is_file()
    report["control_credential_owner_only"] = (
        token.is_file() and stat.S_IMODE(token.stat().st_mode) == 0o600
    )
    health = probe_health(config)
    report["daemon"] = {
        "reachable": health is not None,
        "state": health.get("state") if health is not None else None,
        "ready": health.get("ready") if health is not None else False,
    }
    usable = bool(
        report["config_owner_only"]
        and report["database_present"]
        and report["control_credential_owner_only"]
    )
    report["usable"] = usable
    return report, usable


def run_doctor(
    explicit_config: Path | None,
    *,
    print_fn: Callable[[str], None] = print,
) -> int:
    report, usable = doctor_report(explicit_config)
    print_fn(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if usable else 1


__all__ = [
    "SetupResult",
    "discover_git_root",
    "doctor_report",
    "initialize_default_workspace",
    "run_doctor",
    "run_setup",
    "setup_payload",
    "write_new_config",
]
