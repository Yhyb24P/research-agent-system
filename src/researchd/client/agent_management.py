"""Trusted product-level Agent onboarding for Developer Preview."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from researchd.bridge.aweswitch_agent import (
    AweswitchProfileError,
    default_aweswitch_config,
    load_profile_metadata,
)
from researchd.client.lifecycle import (
    base_url_for,
    load_client_config,
    probe_health,
    researchd_argv,
    spawn_daemon,
    stop_daemon,
    wait_for_ready,
)
from researchd.client.transport import ResearchClient, load_owner_token
from researchd.collaboration.agent_definitions import AgentDefinition
from researchd.collaboration.contracts import AgentProfile, AgentRuntime
from researchd.domain.enums import AgentAdapterKind, AgentTrustZone
from researchd.domain.ids import AgentId, AgentRuntimeId
from researchd.runtime_sessions.contracts import (
    LaunchMode,
    ProcessLaunchConfiguration,
    ProcessLaunchSpec,
    RuntimeLaunchProfile,
)
from researchd.runtime_sessions.launch_profiles import RuntimeLaunchProfileService

PreviewRole = Literal["planner", "coder", "reviewer"]

_MANAGED_AGENT_TIMEOUT_SECONDS = 600
_ROLE_CONTRACTS: dict[PreviewRole, tuple[str, tuple[str, ...], int]] = {
    "planner": ("planner", ("plan.propose", "evidence.request"), 19011),
    "coder": (
        "executor",
        ("code.inspect", "code.modify", "test.execute", "artifact.publish"),
        19003,
    ),
    "reviewer": ("reviewer", ("evidence.review", "decision.propose"), 19012),
}


def discover_aweswitch_profiles(config_path: Path) -> list[dict[str, object]]:
    """List non-secret profile identities without expanding profile env values."""
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AweswitchProfileError("aweswitch config is unavailable or invalid") from error
    profiles = document.get("profiles") if isinstance(document, dict) else None
    if not isinstance(profiles, dict):
        raise AweswitchProfileError("aweswitch config has no profiles object")
    result: list[dict[str, object]] = []
    for provider, entries in sorted(profiles.items()):
        if not isinstance(provider, str) or not isinstance(entries, dict):
            continue
        for name, profile in sorted(entries.items()):
            if not isinstance(name, str) or not isinstance(profile, dict):
                continue
            result.append({
                "profile": name,
                "provider": provider,
                "managed_bridge_supported": provider == "qwen",
            })
    return result


def default_profile_ref() -> str | None:
    """Return the sole supported aweswitch profile, if selection is unambiguous."""
    try:
        profiles = discover_aweswitch_profiles(default_aweswitch_config())
    except AweswitchProfileError:
        return None
    supported = [
        str(item["profile"])
        for item in profiles
        if item["managed_bridge_supported"] is True
    ]
    return f"aweswitch:{supported[0]}" if len(supported) == 1 else None


def build_aweswitch_definition(
    role: PreviewRole,
    *,
    profile: str,
    project_root: Path,
    aweswitch: Path,
    qwen: Path,
    aweswitch_config: Path,
) -> AgentDefinition:
    """Generate a referentially closed definition containing no credentials."""
    controller_role, skills, port = _ROLE_CONTRACTS[role]
    agent_id = AgentId(f"agent_{role}")
    runtime_id = AgentRuntimeId(f"runtime_{role}_aweswitch")
    endpoint = f"http://127.0.0.1:{port}/invoke"
    # Keep the environment's interpreter path intact.  Resolving a virtualenv
    # symlink to the system Python discards the environment that contains
    # ``researchd`` and makes the managed bridge exit before binding its port.
    executable = Path(sys.executable).absolute()
    if not executable.is_file():
        raise ValueError("current Python executable is unavailable")
    launch_spec = ProcessLaunchSpec(
        argv=(
            str(executable),
            "-m",
            "researchd.bridge.aweswitch_agent",
            "--profile",
            profile,
            "--aweswitch",
            str(aweswitch),
            "--qwen",
            str(qwen),
            "--config",
            str(aweswitch_config),
            "--cwd",
            str(project_root),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--timeout",
            str(_MANAGED_AGENT_TIMEOUT_SECONDS),
        ),
        cwd=str(project_root),
    )
    configuration = ProcessLaunchConfiguration(
        launch_spec=launch_spec
    ).model_dump(mode="json")
    digest = RuntimeLaunchProfileService._digest(LaunchMode.PROCESS, configuration)
    return AgentDefinition(
        profile=AgentProfile(
            agent_id=agent_id,
            display_name=role.title(),
            roles=(controller_role,),
            skills=skills,
            trust_zone=AgentTrustZone.LOCAL_PRIVATE,
            constraints=("invocation_required", "controller_owned_delegation"),
            labels={
                "cli_alias": role,
                "profile_provider": "aweswitch",
                "profile_ref": f"aweswitch:{profile}",
            },
        ),
        runtimes=(AgentRuntime(
            runtime_id=runtime_id,
            agent_id=agent_id,
            adapter_kind=AgentAdapterKind.PROCESS,
            runtime_name=f"{role.title()} aweswitch bridge",
            endpoint_ref=endpoint,
            framework="research-agent-json-v1",
            model_provider="aweswitch",
            model_name=profile,
            protocols=("research-agent-json-v1",),
            metadata={"health_endpoint": f"http://127.0.0.1:{port}/health"},
        ),),
        launch_profiles=(RuntimeLaunchProfile(
            runtime_id=runtime_id,
            launch_mode=LaunchMode.PROCESS,
            configuration=configuration,
            spec_sha256=digest,
        ),),
    )


def _project_root(config_path: Path) -> Path:
    config = load_client_config(config_path)
    configured = config.repositories.get("project")
    if configured is None and len(config.repositories) == 1:
        configured = next(iter(config.repositories.values()))
    if configured is None:
        raise ValueError("config must contain one trusted project repository")
    root = configured.resolve(strict=True)
    if not (root / ".git").exists():
        raise ValueError("configured project is not a Git worktree")
    return root


def _change_registry(
    config_path: Path,
    arguments: list[str],
    *,
    print_fn: Callable[[str], None],
    run_fn: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> bool:
    config = load_client_config(config_path)
    was_running = probe_health(config) is not None
    if was_running and stop_daemon(config_path) != 0:
        print_fn("could not stop the strongly identified researchd instance")
        return False
    completed = run_fn(researchd_argv(config_path, *arguments))
    succeeded = completed.returncode == 0
    if not succeeded:
        print_fn(f"researchd {' '.join(arguments[:1])} failed with exit code {completed.returncode}")
    if was_running:
        spawn_daemon(config, config_path)
        try:
            wait_for_ready(config)
        except (RuntimeError, TimeoutError) as error:
            print_fn(f"researchd restart failed: {error}")
            return False
    return succeeded


def _ready_client(config_path: Path) -> ResearchClient:
    config = load_client_config(config_path)
    health = probe_health(config)
    if health is None:
        spawn_daemon(config, config_path)
        health = wait_for_ready(config)
    elif health.get("ready") is not True:
        health = wait_for_ready(config)
    del health
    return ResearchClient(base_url_for(config), load_owner_token(config.state_root))


def add_aweswitch_agent(
    config_path: Path,
    role: PreviewRole,
    profile_ref: str,
    *,
    print_fn: Callable[[str], None] = print,
) -> int:
    """Install, start and health-gate one generated managed Agent."""
    prefix = "aweswitch:"
    if not profile_ref.startswith(prefix) or len(profile_ref) == len(prefix):
        print_fn("profile must use aweswitch:<profile>")
        return 1
    profile = profile_ref.removeprefix(prefix)
    executable_name = shutil.which("aweswitch")
    if executable_name is None:
        print_fn("aweswitch is not installed")
        return 1
    aweswitch = Path(executable_name).resolve(strict=True)
    qwen_name = shutil.which("qwen")
    if qwen_name is None:
        print_fn("qwen is not installed")
        return 1
    qwen = Path(qwen_name).absolute()
    profile_config = default_aweswitch_config().resolve(strict=True)
    try:
        load_profile_metadata(profile_config, profile)
        definition = build_aweswitch_definition(
            role,
            profile=profile,
            project_root=_project_root(config_path),
            aweswitch=aweswitch,
            qwen=qwen,
            aweswitch_config=profile_config,
        )
    except (AweswitchProfileError, OSError, ValueError) as error:
        print_fn(f"cannot build Agent definition: {error}")
        return 1
    config = load_client_config(config_path)
    config.state_root.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f"definition-{role}-", suffix=".json", dir=config.state_root,
    )
    definition_path = Path(name)
    try:
        os.chmod(definition_path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(definition.model_dump_json(indent=2))
            handle.write("\n")
        if not _change_registry(
            config_path,
            ["install-agent", str(definition_path)],
            print_fn=print_fn,
        ):
            return 1
    finally:
        definition_path.unlink(missing_ok=True)
    try:
        with _ready_client(config_path) as client:
            envelope = client.post_command(f"/api/agents/agent_{role}/start", {})
    except Exception as error:
        print_fn(f"Agent installed but failed to start: {type(error).__name__}")
        return 1
    print_fn(f"{role} installed from {profile_ref}: {envelope['status']}")
    return 0


def install_aweswitch_agents_for_setup(
    config_path: Path,
    roles: tuple[PreviewRole, ...],
    profile_ref: str,
    *,
    run_fn: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> None:
    """Install generated definitions while first-run researchd is still stopped."""
    prefix = "aweswitch:"
    if not profile_ref.startswith(prefix) or len(profile_ref) == len(prefix):
        raise ValueError("profile must use aweswitch:<profile>")
    profile = profile_ref.removeprefix(prefix)
    executable_name = shutil.which("aweswitch")
    if executable_name is None:
        raise ValueError("aweswitch is not installed")
    aweswitch = Path(executable_name).resolve(strict=True)
    qwen_name = shutil.which("qwen")
    if qwen_name is None:
        raise ValueError("qwen is not installed")
    qwen = Path(qwen_name).absolute()
    profile_config = default_aweswitch_config().resolve(strict=True)
    load_profile_metadata(profile_config, profile)
    project_root = _project_root(config_path)
    config = load_client_config(config_path)
    config.state_root.mkdir(parents=True, exist_ok=True)
    for role in roles:
        definition = build_aweswitch_definition(
            role,
            profile=profile,
            project_root=project_root,
            aweswitch=aweswitch,
            qwen=qwen,
            aweswitch_config=profile_config,
        )
        descriptor, name = tempfile.mkstemp(
            prefix=f"definition-{role}-", suffix=".json", dir=config.state_root,
        )
        definition_path = Path(name)
        try:
            os.chmod(definition_path, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(definition.model_dump_json(indent=2))
                handle.write("\n")
            completed = run_fn(researchd_argv(
                config_path, "install-agent", str(definition_path),
            ))
            if completed.returncode != 0:
                raise RuntimeError(f"failed to install {role} AgentDefinition")
        finally:
            definition_path.unlink(missing_ok=True)


def remove_agent(
    config_path: Path,
    role: PreviewRole,
    *,
    print_fn: Callable[[str], None] = print,
) -> int:
    succeeded = _change_registry(
        config_path,
        ["remove-agent", f"agent_{role}"],
        print_fn=print_fn,
    )
    if succeeded:
        print_fn(f"disabled agent_{role}; durable history was preserved")
    return 0 if succeeded else 1


def list_agents(
    config_path: Path,
    *,
    print_fn: Callable[[str], None] = print,
) -> int:
    try:
        with _ready_client(config_path) as client:
            agents = client.get("/api/agents")
    except Exception as error:
        print_fn(f"cannot list Agents: {type(error).__name__}")
        return 1
    if not agents:
        print_fn("no installed Agents; use `research agent add coder --profile aweswitch:<profile>`")
        return 0
    for agent in agents:
        print_fn(
            f"{agent['agent_id']}  enabled={agent['enabled']}  "
            f"roles={','.join(agent['roles'])}  {agent['display_name']}"
        )
    return 0


__all__ = [
    "PreviewRole",
    "add_aweswitch_agent",
    "build_aweswitch_definition",
    "default_profile_ref",
    "discover_aweswitch_profiles",
    "list_agents",
    "install_aweswitch_agents_for_setup",
    "remove_agent",
]
