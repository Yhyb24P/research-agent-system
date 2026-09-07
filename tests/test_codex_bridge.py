"""Round 3: Codex bridge, Qwen bridge regression, agent onboarding, TUI layout.

Covers the round-3 acceptance items:

- /shell codex resolution (unique / 0-match / 2-match / payload purity)
- Codex bridge (profile discovery, fixed command, fail-closed, secret isolation)
- Qwen bridge regression (event-array decoding, shared response, no --qwen)
- Agent onboarding (definition provider, launch catalog, secret isolation)
- Fixed input box (output growth, resize, tab switch, dynamic pane)
"""

import json
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, cast

import pytest

from researchd.bridge.aweswitch_agent import (
    AweswitchManagedBridge,
    AweswitchProfileError,
    build_aweswitch_environment,
    load_profile_metadata,
)
from researchd.collaboration.heterogeneous import (
    ManagedAgentTurnRequest,
    ManagedAgentTurnResponse,
)
from researchd.domain.enums import DelegationPurpose


# ------------------------------------------------------------------
# Fake aweswitch helper
# ------------------------------------------------------------------

_FAKE_AWESWITCH_SCRIPT = '''#!/usr/bin/env python3
import sys, json, os, time

args = sys.argv[1:]
output_last_message = None
is_qwen_format = False
for i, arg in enumerate(args):
    if arg == "--output-last-message" and i + 1 < len(args):
        output_last_message = args[i + 1]
    if arg == "-o" and i + 1 < len(args) and args[i + 1] == "json":
        is_qwen_format = True

prompt = sys.stdin.read()

# Record the command line for inspection.
config_path = os.environ.get("AWESWITCH_CONFIG", "")
cmdline_file = config_path + ".cmdline"
if cmdline_file:
    with open(cmdline_file, "w", encoding="utf-8") as f:
        f.write(" ".join(args))

# Read the mode from a file derived from AWESWITCH_CONFIG.
mode_file = config_path + ".mode"
mode = "success"
if os.path.exists(mode_file):
    with open(mode_file, encoding="utf-8") as f:
        mode = f.read().strip()

if is_qwen_format:
    # Qwen format: write event array to stdout.
    if mode == "success":
        event_array = [
            {"type": "result", "subtype": "success", "is_error": False,
             "permission_denials": [], "result": json.dumps({
                 "execution": {"actions": [], "final_claim": "done"},
                 "output": None,
                 "agent_actions": [],
             })},
        ]
        sys.stdout.write(json.dumps(event_array))
        sys.exit(0)
    elif mode == "missing_result":
        sys.stdout.write(json.dumps([]))
        sys.exit(0)
    elif mode == "illegal_json":
        sys.stdout.write("{invalid json")
        sys.exit(0)
    elif mode == "schema_invalid":
        event_array = [
            {"type": "result", "subtype": "success", "is_error": False,
             "permission_denials": [], "result": json.dumps({"wrong": "schema"})},
        ]
        sys.stdout.write(json.dumps(event_array))
        sys.exit(0)
    elif mode == "nonzero_exit":
        # Emit identifiable markers so a leak test can prove neither
        # stdout nor stderr content enters the error projection.
        sys.stdout.write("stdout-leak-marker-XYZ")
        sys.stderr.write("stderr-leak-marker-XYZ")
        sys.exit(1)
    elif mode == "timeout":
        time.sleep(30)
        sys.exit(0)
else:
    # Codex format: write result to --output-last-message file.
    if mode == "success":
        response = {
            "execution": {"actions": [], "final_claim": "done"},
            "output": None,
            "agent_actions": [],
        }
        with open(output_last_message, "w", encoding="utf-8") as f:
            json.dump(response, f)
        sys.exit(0)
    elif mode == "missing_result":
        sys.exit(0)
    elif mode == "over_limit":
        with open(output_last_message, "w", encoding="utf-8") as f:
            f.write("x" * 2_000_000)
        sys.exit(0)
    elif mode == "illegal_json":
        with open(output_last_message, "w", encoding="utf-8") as f:
            f.write("{invalid json")
        sys.exit(0)
    elif mode == "schema_invalid":
        with open(output_last_message, "w", encoding="utf-8") as f:
            json.dump({"wrong": "schema"}, f)
        sys.exit(0)
    elif mode == "nonzero_exit":
        sys.stdout.write("stdout-leak-marker-XYZ")
        sys.stderr.write("stderr-leak-marker-XYZ")
        sys.exit(1)
    elif mode == "timeout":
        time.sleep(30)
        sys.exit(0)
'''


def _setup_bridge_env(
    tmp_path: Path,
    *,
    provider: str = "codex",
    profile: str = "codex-profile",
    mode: str = "success",
) -> dict[str, Path | str]:
    """Set up a fake aweswitch environment for bridge testing."""
    aweswitch = tmp_path / "fake_aweswitch"
    aweswitch.write_text(_FAKE_AWESWITCH_SCRIPT, encoding="utf-8")
    aweswitch.chmod(aweswitch.stat().st_mode | stat.S_IEXEC)

    agent_cli = tmp_path / "fake_agent_cli"
    agent_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    agent_cli.chmod(agent_cli.stat().st_mode | stat.S_IEXEC)

    config = tmp_path / "aweswitch_config.json"
    config.write_text(
        json.dumps({
            "profiles": {
                provider: {
                    profile: {
                        "env": {},
                    }
                }
            }
        }),
        encoding="utf-8",
    )

    mode_file = config.parent / (config.name + ".mode")
    mode_file.write_text(mode, encoding="utf-8")

    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)

    return {
        "aweswitch": aweswitch,
        "agent_cli": agent_cli,
        "config": config,
        "cwd": cwd,
        "mode_file": mode_file,
        "cmdline_file": config.parent / (config.name + ".cmdline"),
        "profile": profile,
    }


def _make_turn() -> ManagedAgentTurnRequest:
    return ManagedAgentTurnRequest(
        invocation_id="inv_test",
        run_id="run_test",
        purpose=DelegationPurpose.EXECUTE,
        allowed_capabilities=(),
        payload={},
    )


def _make_bridge(env: dict[str, Path | str], *, timeout: float = 5.0) -> AweswitchManagedBridge:
    return AweswitchManagedBridge(
        aweswitch=cast(Path, env["aweswitch"]),
        agent_cli=cast(Path, env["agent_cli"]),
        config_path=cast(Path, env["config"]),
        profile=str(env["profile"]),
        cwd=cast(Path, env["cwd"]),
        timeout_seconds=timeout,
    )


# ------------------------------------------------------------------
# Item 4: Codex bridge
# ------------------------------------------------------------------


def test_codex_profile_discovery_marks_managed() -> None:
    """Item 4a: profile discovery marks codex as a managed provider."""
    with tempfile.TemporaryDirectory() as d:
        tmp_path = Path(d)
        config = tmp_path / "aweswitch_config.json"
        config.write_text(
            json.dumps({
                "profiles": {
                    "codex": {
                        "codex-profile": {"env": {}}
                    }
                }
            }),
            encoding="utf-8",
        )
        provider, metadata = load_profile_metadata(config, "codex-profile")
        assert provider == "codex"
        assert metadata["provider"] == "codex"
        assert metadata["profile"] == "codex-profile"


def test_codex_fixed_command_includes_required_flags(tmp_path: Path) -> None:
    """Item 4b: the fixed command is `aweswitch <profile> -- exec` with
    the ephemeral/read-only/output-schema flags, in that order."""
    env = _setup_bridge_env(tmp_path, mode="success")
    bridge = _make_bridge(env)
    bridge.invoke(_make_turn())
    # Inspect the recorded command line.
    cmdline = cast(Path, env["cmdline_file"]).read_text(encoding="utf-8")
    assert "codex-profile -- exec --ephemeral --sandbox read-only --output-schema " in cmdline
    assert "--output-last-message " in cmdline
    assert cmdline.rstrip().endswith("--json -")


def test_codex_schema_valid_final_message_succeeds(tmp_path: Path) -> None:
    """Item 4c: a schema-valid final message produces a valid response."""
    env = _setup_bridge_env(tmp_path, mode="success")
    bridge = _make_bridge(env)
    response = bridge.invoke(_make_turn())
    assert isinstance(response, ManagedAgentTurnResponse)
    assert response.execution is not None
    assert response.execution.final_claim == "done"


@pytest.mark.parametrize(
    "mode,expected_fragment",
    [
        ("missing_result", "result is unavailable"),
        ("over_limit", "exceeds limit"),
        ("illegal_json", "invalid managed JSON"),
        ("schema_invalid", "invalid managed JSON"),
        ("nonzero_exit", "exit code 1"),
    ],
)
def test_codex_fail_closed_modes(
    tmp_path: Path, mode: str, expected_fragment: str
) -> None:
    """Item 4d: all failure modes fail closed with safe diagnostics."""
    env = _setup_bridge_env(tmp_path, mode=mode)
    bridge = _make_bridge(env)
    with pytest.raises(AweswitchProfileError, match=expected_fragment):
        bridge.invoke(_make_turn())


def test_codex_timeout_fails_closed(tmp_path: Path) -> None:
    """Item 4e: a timeout fails closed."""
    env = _setup_bridge_env(tmp_path, mode="timeout")
    bridge = _make_bridge(env, timeout=1.0)
    with pytest.raises(AweswitchProfileError, match="timed out"):
        bridge.invoke(_make_turn())


def test_codex_error_does_not_leak_secret_or_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 4f: stdout/stderr content and the profile secret never enter
    the error projection.

    The profile env references ${OPENAI_API_KEY}, so the child process
    genuinely receives the secret value in its environment; the fake
    aweswitch also emits identifiable stdout/stderr markers before
    failing.  None of that may appear in the raised error.
    """
    env = _setup_bridge_env(tmp_path, mode="nonzero_exit")
    config = cast(Path, env["config"])
    config.write_text(
        json.dumps({
            "profiles": {
                "codex": {
                    "codex-profile": {
                        "env": {"OPENAI_API_KEY": "${OPENAI_API_KEY}"},
                    }
                }
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret-value")
    bridge = _make_bridge(env)
    with pytest.raises(AweswitchProfileError) as exc_info:
        bridge.invoke(_make_turn())
    message = str(exc_info.value)
    for leaked in (
        "sk-test-secret-value",
        "OPENAI_API_KEY",
        "${OPENAI_API_KEY}",
        "stdout-leak-marker-XYZ",
        "stderr-leak-marker-XYZ",
    ):
        assert leaked not in message, f"error projection leaked {leaked!r}"


def test_codex_profile_secret_not_in_environment(tmp_path: Path) -> None:
    """Item 4g: the profile secret reference is not expanded into the child environment."""
    env = _setup_bridge_env(tmp_path)
    # Create a profile with an env reference to test secret isolation.
    config = cast(Path, env["config"])
    config.write_text(
        json.dumps({
            "profiles": {
                "codex": {
                    "codex-profile": {
                        "env": {"API_KEY": "${MY_API_KEY}"},
                    }
                }
            }
        }),
        encoding="utf-8",
    )
    # Pass a custom environ that includes the referenced variable.
    child_env = build_aweswitch_environment(
        config, "codex-profile", environ={"MY_API_KEY": "secret-value"}
    )
    # The referenced variable is included (needed by the launcher), but the
    # profile's env value (${MY_API_KEY}) is not expanded into the environment.
    assert "API_KEY" not in child_env
    assert "${MY_API_KEY}" not in child_env.values()


# ------------------------------------------------------------------
# Item 5: Qwen bridge regression
# ------------------------------------------------------------------


def test_qwen_profile_discovery_marks_managed() -> None:
    """Item 5a: qwen profile discovery marks qwen as a managed provider."""
    with tempfile.TemporaryDirectory() as d:
        tmp_path = Path(d)
        config = tmp_path / "aweswitch_config.json"
        config.write_text(
            json.dumps({
                "profiles": {
                    "qwen": {
                        "qwen-profile": {"env": {}}
                    }
                }
            }),
            encoding="utf-8",
        )
        provider, metadata = load_profile_metadata(config, "qwen-profile")
        assert provider == "qwen"
        assert metadata["provider"] == "qwen"


def test_qwen_event_array_decoding_unchanged(tmp_path: Path) -> None:
    """Item 5b: the existing event-array decoding path is unchanged.

    The qwen bridge uses the _decode method which expects an event array
    with exactly one terminal result event.
    """
    from researchd.bridge.aweswitch_agent import AweswitchManagedBridge

    env = _setup_bridge_env(tmp_path, provider="qwen", profile="qwen-profile", mode="success")
    bridge = _make_bridge(env)
    # The qwen bridge uses the same ManagedAgentTurnResponse.
    response = bridge.invoke(_make_turn())
    assert isinstance(response, ManagedAgentTurnResponse)


def test_qwen_and_codex_share_managed_response(tmp_path: Path) -> None:
    """Item 5c: qwen and codex bridges both return the same
    ManagedAgentTurnResponse model."""
    from researchd.executor.contracts import LocalAgentResponse

    qwen_env = _setup_bridge_env(tmp_path, provider="qwen", profile="qwen-profile")
    qwen_response = _make_bridge(qwen_env).invoke(_make_turn())
    # Reuse the same sandbox for the codex provider; the setup rewrites
    # the config and mode files in place.
    codex_env = _setup_bridge_env(tmp_path, provider="codex", profile="codex-profile")
    codex_response = _make_bridge(codex_env).invoke(_make_turn())

    assert type(qwen_response) is ManagedAgentTurnResponse
    assert type(codex_response) is ManagedAgentTurnResponse
    # The response model keeps exactly one result kind.
    assert qwen_response.execution is not None
    assert qwen_response.output is None
    assert qwen_response.agent_actions == ()
    expected = ManagedAgentTurnResponse(
        execution=LocalAgentResponse(actions=(), final_claim="done"),
        output=None,
        agent_actions=(),
    )
    assert qwen_response == expected
    assert codex_response == expected


def test_no_legacy_qwen_compat_entry() -> None:
    """Item 5d: no old --qwen compat entry is introduced."""
    import inspect
    from researchd.bridge import aweswitch_agent

    # The bridge constructor takes a provider-neutral agent CLI.
    parameters = inspect.signature(AweswitchManagedBridge.__init__).parameters
    assert "agent_cli" in parameters
    assert "qwen" not in parameters
    # The CLI entry exposes --agent-cli, never a --qwen flag.
    main_source = inspect.getsource(aweswitch_agent.main)
    assert '"--agent-cli"' in main_source
    assert "--qwen" not in main_source
    assert '"--qwen"' not in inspect.getsource(aweswitch_agent)
    assert "'--qwen'" not in inspect.getsource(aweswitch_agent)


# ------------------------------------------------------------------
# Item 3: /shell codex resolution
# ------------------------------------------------------------------


def _codex_agent(agent_id: str = "agent_coder", display: str = "Coder") -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "display_name": display,
        "enabled": True,
        "runtimes": [
            {
                "runtime_id": f"rt_{agent_id}",
                "model_provider": "codex",
                "model_name": "codex-1",
                "adapter_kind": "AWESWITCH",
                "enabled": True,
            }
        ],
    }


def _qwen_agent() -> dict[str, Any]:
    return {
        "agent_id": "agent_qwen",
        "display_name": "Qwen",
        "enabled": True,
        "runtimes": [
            {
                "runtime_id": "rt_qwen",
                "model_provider": "qwen",
                "model_name": "qwen-1",
                "adapter_kind": "AWESWITCH",
                "enabled": True,
            }
        ],
    }


class _AgentClient:
    """Records POSTs; serves a fixed agent listing."""

    def __init__(self, agents: list[dict[str, Any]]) -> None:
        self.agents = agents
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def health(self) -> dict[str, Any]:
        return {"state": "READY", "ready": True}

    def get(self, path: str, **kwargs: Any) -> Any:
        if path == "/api/agents":
            return list(self.agents)
        return []

    def post_command(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        self.posts.append((path, dict(payload or {})))
        return {"status": "ACCEPTED", "command_id": command_id or "cmd_test"}

    def stream(self, path: str, **kwargs: Any) -> Any:
        return iter(())


def test_shell_codex_unique_runtime_resolves_and_starts() -> None:
    """Item 3a: when the registry has a unique Codex runtime, `agent start
    codex` resolves it and dispatches the start command."""
    from researchd.client.shell import ShellSession, resolve_agent_reference

    client = _AgentClient([_codex_agent()])
    resolved = resolve_agent_reference(client.agents, "codex")
    assert resolved["agent_id"] == "agent_coder"
    session = ShellSession(cast(Any, client))
    assert session.execute("agent start codex") is True
    assert client.posts == [("/api/agents/agent_coder/start", {})]


def test_shell_codex_zero_match_fails() -> None:
    """Item 3b: 0 matching runtimes -> resolution fails, zero dispatch."""
    from researchd.client.shell import ShellParseError, ShellSession, resolve_agent_reference

    client = _AgentClient([_qwen_agent()])
    with pytest.raises(ShellParseError, match="unknown agent reference"):
        resolve_agent_reference(client.agents, "codex")
    # Through the real session the failure surfaces as a shell error and
    # no HTTP dispatch happens.
    outputs: list[str] = []
    session = ShellSession(cast(Any, client), print_fn=outputs.append)
    assert session.execute("agent start codex") is True
    assert any("unknown agent reference" in line for line in outputs)
    assert client.posts == []


def test_shell_codex_two_match_ambiguous_fails() -> None:
    """Item 3c: two agents with Codex runtimes -> ambiguous, zero dispatch.

    Two runtimes inside one agent still resolve to that agent (ambiguity
    is at the agent level); two different agents are ambiguous.
    """
    from researchd.client.shell import ShellParseError, ShellSession, resolve_agent_reference

    single_agent = _codex_agent()
    single_agent["runtimes"].append({
        "runtime_id": "rt_codex_2",
        "model_provider": "codex",
        "model_name": "codex-2",
        "adapter_kind": "AWESWITCH",
        "enabled": True,
    })
    assert resolve_agent_reference([single_agent], "codex")["agent_id"] == "agent_coder"

    agents_ambiguous = [_codex_agent("agent_a", "A"), _codex_agent("agent_b", "B")]
    with pytest.raises(ShellParseError, match="ambiguous agent reference"):
        resolve_agent_reference(agents_ambiguous, "codex")
    client = _AgentClient(agents_ambiguous)
    outputs: list[str] = []
    session = ShellSession(cast(Any, client), print_fn=outputs.append)
    assert session.execute("agent start codex") is True
    assert any("ambiguous agent reference" in line for line in outputs)
    assert client.posts == []


def test_shell_codex_payload_is_empty() -> None:
    """Item 3d: the dispatched start payload is empty.

    Drive the real ShellSession so the assertion covers the payload that
    actually leaves the client, not a locally constructed dict.
    """
    from researchd.client.shell import ShellSession

    client = _AgentClient([_codex_agent()])
    session = ShellSession(cast(Any, client))
    assert session.execute("agent start codex") is True
    assert len(client.posts) == 1
    path, payload = client.posts[0]
    assert path == "/api/agents/agent_coder/start"
    assert payload == {}
    forbidden = {"argv", "cwd", "actor", "runtime_id", "runtime_session_id", "session_id"}
    for key in forbidden:
        assert key not in payload, f"payload must not contain {key!r}"


# ------------------------------------------------------------------
# Item 6: Agent onboarding
# ------------------------------------------------------------------


def test_agent_add_codex_definition_has_provider(tmp_path: Path) -> None:
    """Item 6a: `research agent add coder --profile aweswitch:<codex-profile>`
    generates a Definition with model_provider=codex."""
    from researchd.client.agent_management import build_aweswitch_definition

    env = _setup_bridge_env(tmp_path, provider="codex", profile="codex-profile")
    definition = build_aweswitch_definition(
        "coder",
        profile="codex-profile",
        project_root=tmp_path,
        aweswitch=cast(Path, env["aweswitch"]),
        agent_cli=cast(Path, env["agent_cli"]),
        provider="codex",
        aweswitch_config=cast(Path, env["config"]),
    )
    runtime = definition.runtimes[0]
    assert runtime.model_provider == "codex"
    assert runtime.model_name == "codex-profile"
    assert definition.profile.labels["profile_provider"] == "codex"
    assert definition.profile.labels["profile_ref"] == "aweswitch:codex-profile"


def test_agent_add_codex_flow_installs_provider_definition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 6: the full `agent add coder --profile aweswitch:<codex-profile>`
    flow installs a Definition with model_provider=codex, a launch catalog
    of trusted absolute CLI paths, and an empty start payload.

    The daemon-touching seams (registry change, ready client, PATH lookup,
    project root) are replaced; the definition builder and the onboarding
    orchestration itself run for real.
    """
    import shutil
    import subprocess

    import researchd.client.agent_management as agent_management
    from researchd.daemon.composition import DaemonConfig

    aweswitch = tmp_path / "aweswitch-bin"
    aweswitch.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    aweswitch.chmod(0o700)
    codex_cli = tmp_path / "codex-bin"
    codex_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex_cli.chmod(0o700)

    def fake_which(name: str) -> str | None:
        if name == "aweswitch":
            return str(aweswitch)
        if name == "codex":
            return str(codex_cli)
        return None

    # agent_management resolves the CLIs through the shared shutil module.
    monkeypatch.setattr(shutil, "which", fake_which)

    profile_config = tmp_path / "aweswitch_config.json"
    profile_config.write_text(
        json.dumps({"profiles": {"codex": {"codex-profile": {"env": {}}}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        agent_management, "default_aweswitch_config", lambda: profile_config,
    )

    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    monkeypatch.setattr(agent_management, "_project_root", lambda config_path: project)

    captured: dict[str, Any] = {}

    def fake_change_registry(
        config_path: Path,
        arguments: list[str],
        *,
        print_fn: Any,
        run_fn: Any = subprocess.run,
    ) -> bool:
        captured["definition"] = Path(arguments[1]).read_text(encoding="utf-8")
        return True

    monkeypatch.setattr(agent_management, "_change_registry", fake_change_registry)

    state_root = tmp_path / "state"
    state_root.mkdir()
    daemon_config = DaemonConfig(
        database=tmp_path / "db.sqlite3",
        artifact_root=tmp_path / "artifacts",
        state_root=state_root,
    )
    monkeypatch.setattr(
        agent_management, "load_client_config", lambda config_path: daemon_config,
    )

    class _FakeClient:
        def __enter__(self) -> "_FakeClient":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def post_command(
            self,
            path: str,
            payload: dict[str, Any] | None = None,
            *,
            command_id: str | None = None,
        ) -> dict[str, Any]:
            captured["start"] = (path, dict(payload or {}))
            return {"status": "ACCEPTED", "command_id": command_id or "cmd_ok"}

    monkeypatch.setattr(agent_management, "_ready_client", lambda config_path: _FakeClient())

    outputs: list[str] = []
    exit_code = agent_management.add_aweswitch_agent(
        tmp_path / "client.json", "coder", "aweswitch:codex-profile",
        print_fn=outputs.append,
    )
    assert exit_code == 0
    assert any("installed from aweswitch:codex-profile" in line for line in outputs)

    definition = json.loads(captured["definition"])
    assert definition["runtimes"][0]["model_provider"] == "codex"
    assert definition["runtimes"][0]["model_name"] == "codex-profile"
    launch_spec = cast(dict[str, object], definition["launch_profiles"][0]["configuration"]["launch_spec"])
    argv = [str(item) for item in cast(list[str], launch_spec["argv"])]
    assert Path(argv[0]).is_absolute()
    for index, token in enumerate(argv):
        if token in {"--aweswitch", "--agent-cli", "--config", "--cwd"}:
            assert Path(argv[index + 1]).is_absolute(), f"{token} must be absolute"
    assert argv[argv.index("--agent-cli") + 1] == str(codex_cli)

    start_path, start_payload = captured["start"]
    assert start_path == "/api/agents/agent_coder/start"
    assert start_payload == {}


def test_agent_add_profile_secret_not_in_definition(tmp_path: Path) -> None:
    """Item 6c: the profile secret doesn't enter Definition/Registry/audit."""
    from researchd.client.agent_management import build_aweswitch_definition

    env = _setup_bridge_env(tmp_path)
    # Create a profile with an env reference to test secret isolation.
    config = cast(Path, env["config"])
    config.write_text(
        json.dumps({
            "profiles": {
                "codex": {
                    "codex-profile": {
                        "env": {"API_KEY": "${MY_API_KEY}"},
                    }
                }
            }
        }),
        encoding="utf-8",
    )
    # Build the Definition and verify the secret is not in it.
    definition = build_aweswitch_definition(
        "coder",
        profile="codex-profile",
        project_root=tmp_path,
        aweswitch=cast(Path, env["aweswitch"]),
        agent_cli=cast(Path, env["agent_cli"]),
        provider="codex",
        aweswitch_config=config,
    )
    # The Definition is what the Registry stores and the audit trail cites;
    # neither the env key, the ${...} reference, nor any secret value may
    # appear in its serialization.
    serialized = json.dumps(definition.model_dump(mode="json"), sort_keys=True)
    assert "API_KEY" not in serialized
    assert "${" not in serialized
    assert "MY_API_KEY" not in serialized
    # The model_provider is set correctly.
    assert definition.runtimes[0].model_provider == "codex"


def test_launch_catalog_contains_only_trusted_paths(tmp_path: Path) -> None:
    """Item 6b: the launch catalog carries only trusted absolute paths;
    the Agent CLI is the installer-resolved absolute path, not a bare
    command name."""
    from researchd.client.agent_management import build_aweswitch_definition

    env = _setup_bridge_env(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    definition = build_aweswitch_definition(
        "coder",
        profile="codex-profile",
        project_root=project,
        aweswitch=cast(Path, env["aweswitch"]),
        agent_cli=cast(Path, env["agent_cli"]),
        provider="codex",
        aweswitch_config=cast(Path, env["config"]),
    )
    configuration = definition.launch_profiles[0].configuration
    launch_spec = cast(dict[str, object], configuration["launch_spec"])
    argv = [str(item) for item in cast(list[str], launch_spec["argv"])]
    # The executable and the working directory are absolute.
    assert Path(argv[0]).is_absolute()
    assert Path(str(launch_spec["cwd"])).is_absolute()
    # Every path-bearing flag resolves to an absolute path.
    for index, token in enumerate(argv):
        if token in {"--aweswitch", "--agent-cli", "--config", "--cwd"}:
            value = argv[index + 1]
            assert Path(value).is_absolute(), f"{token} must be absolute, got {value!r}"
    # The Agent CLI path is exactly the trusted absolute CLI, never "codex".
    assert argv[argv.index("--agent-cli") + 1] == str(cast(Path, env["agent_cli"]))
    assert "codex" not in [token for token in argv if not token.startswith("-")]


# ------------------------------------------------------------------
# Item 7: Fixed input box
# ------------------------------------------------------------------


def test_input_box_stable_across_tab_switch(tmp_path: Path) -> None:
    """Item 7a: switching tabs does not drift the input box coordinates."""
    import asyncio

    from researchd.client.tui_app import ResearchWorkspace
    from researchd.client.transport import ResearchClient
    from textual.widgets import TabbedContent

    class _FakeClient:
        def __init__(self) -> None:
            self.agents: list[dict[str, Any]] = [
                {
                    "agent_id": "agent_coder",
                    "display_name": "Coder",
                    "enabled": True,
                    "runtimes": [],
                },
            ]
            self.posts: list[tuple[str, dict[str, Any]]] = []

        def health(self) -> dict[str, Any]:
            return {"state": "READY", "ready": True}

        def get(self, path: str, **kwargs: Any) -> Any:
            if path == "/api/agents":
                return list(self.agents)
            if path == "/api/runs":
                return []
            if path == "/api/approvals":
                return []
            if path == "/api/handoffs":
                return []
            if path.endswith("/messages"):
                return {"messages": []}
            if "/console" in path:
                agent_id = path.split("/")[3]
                agent = next(
                    (a for a in self.agents if a["agent_id"] == agent_id),
                    {"agent_id": agent_id, "display_name": agent_id, "enabled": True, "runtimes": []},
                )
                return {"agent": agent, "runtime_sessions": [], "invocations": []}
            raise AssertionError(f"unexpected GET {path}")

        def post_command(
            self,
            path: str,
            payload: dict[str, Any] | None = None,
            *,
            command_id: str | None = None,
        ) -> dict[str, Any]:
            self.posts.append((path, dict(payload or {})))
            return {"status": "ACCEPTED", "command_id": command_id or "cmd_test_123"}

        def stream(self, path: str, **kwargs: Any) -> Any:
            return iter(())

    client = _FakeClient()
    app = ResearchWorkspace(cast(ResearchClient, client))

    async def scenario() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.5)
            y_initial = app.query_one("#command-input").region.y
            # The dynamic Agent pane must be mounted before the switch,
            # otherwise the comparison would be vacuous.
            tabs = app.query_one("#workspace-tabs", TabbedContent)
            agent_tab = tabs.get_tab("tab-agent-agent_coder")
            assert agent_tab is not None
            # Switch to the agent tab.
            tabs.active = "tab-agent-agent_coder"
            await pilot.pause(0.3)
            y_after_tab = app.query_one("#command-input").region.y
            assert y_after_tab == y_initial

    asyncio.run(scenario())


def test_input_box_stable_across_dynamic_pane(tmp_path: Path) -> None:
    """Item 7b: dynamically adding an Agent pane does not drift the input box."""
    import asyncio

    from researchd.client.tui_app import ResearchWorkspace
    from researchd.client.transport import ResearchClient
    from textual.widgets import TabbedContent

    class _FakeClient:
        def __init__(self) -> None:
            self.agents: list[dict[str, Any]] = [
                {
                    "agent_id": "agent_coder",
                    "display_name": "Coder",
                    "enabled": True,
                    "runtimes": [],
                },
            ]
            self.posts: list[tuple[str, dict[str, Any]]] = []

        def health(self) -> dict[str, Any]:
            return {"state": "READY", "ready": True}

        def get(self, path: str, **kwargs: Any) -> Any:
            if path == "/api/agents":
                return list(self.agents)
            if path == "/api/runs":
                return []
            if path == "/api/approvals":
                return []
            if path == "/api/handoffs":
                return []
            if path.endswith("/messages"):
                return {"messages": []}
            if "/console" in path:
                agent_id = path.split("/")[3]
                agent = next(
                    (a for a in self.agents if a["agent_id"] == agent_id),
                    {"agent_id": agent_id, "display_name": agent_id, "enabled": True, "runtimes": []},
                )
                return {"agent": agent, "runtime_sessions": [], "invocations": []}
            raise AssertionError(f"unexpected GET {path}")

        def post_command(
            self,
            path: str,
            payload: dict[str, Any] | None = None,
            *,
            command_id: str | None = None,
        ) -> dict[str, Any]:
            self.posts.append((path, dict(payload or {})))
            return {"status": "ACCEPTED", "command_id": command_id or "cmd_test_123"}

        def stream(self, path: str, **kwargs: Any) -> Any:
            return iter(())

    client = _FakeClient()
    app = ResearchWorkspace(cast(ResearchClient, client))

    async def scenario() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.5)
            y_initial = app.query_one("#command-input").region.y
            tabs = app.query_one("#workspace-tabs", TabbedContent)
            count_before = tabs.tab_count
            # Dynamically add a new agent pane.
            client.agents.append({
                "agent_id": "agent_reviewer",
                "display_name": "Reviewer",
                "enabled": True,
                "runtimes": [],
            })
            app.refresh_projections()
            await pilot.pause(0.5)
            # The pane must actually have been added before the coordinate
            # comparison, otherwise the check would be vacuous.
            assert tabs.tab_count == count_before + 1
            y_after_pane = app.query_one("#command-input").region.y
            assert y_after_pane == y_initial

    asyncio.run(scenario())
