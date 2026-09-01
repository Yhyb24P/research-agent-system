"""Developer Preview generated AgentDefinition coverage."""

import json
from pathlib import Path

from researchd.client.agent_management import (
    build_aweswitch_definition,
    discover_aweswitch_profiles,
)


def test_profile_discovery_returns_only_non_secret_metadata(tmp_path: Path) -> None:
    config = tmp_path / "aweswitch.json"
    config.write_text(json.dumps({
        "profiles": {
            "qwen": {"qw": {"env": {"TOKEN": "secret-value"}}},
            "codex": {"cx": {"env": {"OPENAI_API_KEY": "other-secret"}}},
        },
    }), encoding="utf-8")

    profiles = discover_aweswitch_profiles(config)

    assert profiles == [
        {"profile": "cx", "provider": "codex", "managed_bridge_supported": False},
        {"profile": "qw", "provider": "qwen", "managed_bridge_supported": True},
    ]
    assert "secret-value" not in repr(profiles)
    assert "other-secret" not in repr(profiles)


def test_generated_definition_uses_real_paths_and_profile_reference_only(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    aweswitch = tmp_path / "aweswitch"
    aweswitch.write_text("executable", encoding="utf-8")
    config = tmp_path / "aweswitch.json"
    config.write_text("{}", encoding="utf-8")

    definition = build_aweswitch_definition(
        "coder",
        profile="qw",
        project_root=project.resolve(),
        aweswitch=aweswitch.resolve(),
        qwen=Path("/bin/true"),
        aweswitch_config=config.resolve(),
    )

    payload = definition.model_dump(mode="json")
    encoded = json.dumps(payload)
    assert payload["profile"]["agent_id"] == "agent_coder"
    assert payload["profile"]["roles"] == ["executor"]
    assert payload["profile"]["labels"]["profile_ref"] == "aweswitch:qw"
    assert payload["runtimes"][0]["framework"] == "research-agent-json-v1"
    assert str(aweswitch.resolve()) in encoded
    assert '"--qwen", "/bin/true"' in encoded
    assert str(project.resolve()) in encoded
    assert "TOKEN" not in encoded
