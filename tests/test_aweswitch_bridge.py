"""Structured aweswitch bridge and secret-boundary coverage."""

import json
import os
from pathlib import Path

import pytest

from researchd.bridge.aweswitch_agent import (
    AweswitchManagedBridge,
    AweswitchProfileError,
    build_managed_prompt,
    build_aweswitch_environment,
    load_profile_metadata,
)
from researchd.collaboration.heterogeneous import ManagedAgentTurnRequest
from researchd.domain.enums import DelegationPurpose


def _config(path: Path, profile_env: dict[str, object] | None = None) -> Path:
    path.write_text(json.dumps({
        "profiles": {
            "qwen": {"qw": {"env": profile_env or {"QWEN_MODEL": "qwen-test"}}},
        },
    }), encoding="utf-8")
    return path


def _executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o700)
    return path


def test_profile_metadata_never_returns_profile_environment(tmp_path: Path) -> None:
    config = _config(tmp_path / "aweswitch.json", {
        "QWEN_MODEL": "qwen-test",
        "API_TOKEN": "${QWEN_TEST_SECRET}",
    })

    provider, metadata = load_profile_metadata(
        config,
        "qw",
        environ={"QWEN_TEST_SECRET": "never-return-this"},
    )

    assert provider == "qwen"
    assert metadata == {"profile": "qw", "provider": "qwen"}
    assert "never-return-this" not in repr(metadata)


def test_bridge_environment_only_inherits_safe_and_referenced_names(tmp_path: Path) -> None:
    config = _config(tmp_path / "aweswitch.json", {
        "QWEN_MODEL": "qwen-test",
        "API_TOKEN": "${QWEN_TEST_SECRET}",
    })

    environment = build_aweswitch_environment(config, "qw", environ={
        "PATH": "/usr/bin",
        "HOME": "/home/test",
        "QWEN_TEST_SECRET": "profile-secret",
        "UNRELATED_API_KEY": "must-not-cross",
    })

    assert environment["QWEN_TEST_SECRET"] == "profile-secret"
    assert environment["PATH"] == "/usr/bin"
    assert "UNRELATED_API_KEY" not in environment


def test_missing_referenced_secret_fails_closed(tmp_path: Path) -> None:
    config = _config(tmp_path / "aweswitch.json", {
        "API_TOKEN": "${QWEN_TEST_SECRET}",
    })

    with pytest.raises(AweswitchProfileError, match="QWEN_TEST_SECRET"):
        load_profile_metadata(config, "qw", environ={})


def test_execute_prompt_declares_granted_capability_parameter_contracts() -> None:
    prompt = build_managed_prompt(ManagedAgentTurnRequest(
        invocation_id="inv_prompt",
        run_id="run_prompt",
        purpose=DelegationPurpose.EXECUTE,
        payload={
            "objective": "inspect",
            "prior_results": [],
            "granted_capabilities": ["sandbox.shell"],
        },
    ))

    assert 'CAPABILITY_PARAMETER_CONTRACTS={"sandbox.shell":{"argv":' in prompt
    assert '"arguments"' not in prompt.split("CAPABILITY_PARAMETER_CONTRACTS=", 1)[1]


def test_bridge_validates_outer_cli_json_as_managed_response(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "aweswitch.json")
    response = json.dumps({"execution": {"actions": [], "final_claim": "done"}})
    outer = json.dumps([
        {"type": "system", "subtype": "init"},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "permission_denials": [],
            "result": response,
        },
    ])
    executable = _executable(
        tmp_path / "aweswitch",
        f"printf '%s' '{outer}'\n",
    )
    bridge = AweswitchManagedBridge(
        aweswitch=executable,
        qwen=Path("/bin/true"),
        config_path=config,
        profile="qw",
        cwd=tmp_path,
    )
    turn = ManagedAgentTurnRequest(
        invocation_id="inv_bridge",
        run_id="run_bridge",
        purpose=DelegationPurpose.EXECUTE,
        attempt_id="attempt_bridge",
        payload={
            "objective": "bounded task",
            "prior_results": [],
            "granted_capabilities": [],
        },
    )

    result = bridge.invoke(turn)

    assert result.execution is not None
    assert result.execution.final_claim == "done"


def test_bridge_rejects_unstructured_cli_output(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "aweswitch.json")
    executable = _executable(tmp_path / "aweswitch", "printf '%s' 'not-json'\n")
    bridge = AweswitchManagedBridge(
        aweswitch=executable,
        qwen=Path("/bin/true"),
        config_path=config,
        profile="qw",
        cwd=tmp_path,
    )
    turn = ManagedAgentTurnRequest(
        invocation_id="inv_bridge",
        run_id="run_bridge",
        purpose=DelegationPurpose.PLAN,
        payload={},
    )

    with pytest.raises(AweswitchProfileError, match="invalid managed JSON"):
        bridge.invoke(turn)


@pytest.mark.parametrize(
    "events, message",
    [
        ([], "exactly one terminal result"),
        ([{"type": "result"}, {"type": "result"}], "exactly one terminal result"),
        ([{"type": "result", "subtype": "error_during_execution", "is_error": True}], "not successful"),
        ([{
            "type": "result", "subtype": "success", "is_error": False,
            "permission_denials": [{"tool": "write"}], "result": "{}",
        }], "permission denials"),
    ],
)
def test_bridge_rejects_noncanonical_terminal_event(
    tmp_path: Path,
    events: list[dict[str, object]],
    message: str,
) -> None:
    config = _config(tmp_path / "aweswitch.json")
    executable = _executable(
        tmp_path / "aweswitch",
        f"printf '%s' '{json.dumps(events)}'\n",
    )
    bridge = AweswitchManagedBridge(
        aweswitch=executable,
        qwen=Path("/bin/true"),
        config_path=config,
        profile="qw",
        cwd=tmp_path,
    )
    turn = ManagedAgentTurnRequest(
        invocation_id="inv_bridge",
        run_id="run_bridge",
        purpose=DelegationPurpose.PLAN,
        payload={},
    )

    with pytest.raises(AweswitchProfileError, match=message):
        bridge.invoke(turn)


def test_profile_loader_rejects_unsupported_provider(tmp_path: Path) -> None:
    config = tmp_path / "aweswitch.json"
    config.write_text(json.dumps({
        "profiles": {"codex": {"cx": {"env": {}}}},
    }), encoding="utf-8")

    with pytest.raises(AweswitchProfileError, match="does not yet support"):
        load_profile_metadata(config, "cx", environ=os.environ)
