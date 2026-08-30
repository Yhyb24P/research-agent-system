"""PX01-01: versioned AgentDefinition DTO with a canonical digest."""

import pytest
from pydantic import ValidationError

from researchd.collaboration.agent_definitions import AgentDefinition
from researchd.collaboration.contracts import AgentProfile, AgentRuntime
from researchd.domain.enums import AgentAdapterKind, AgentTrustZone
from researchd.domain.ids import AgentId, AgentRuntimeId
from researchd.runtime_sessions.contracts import LaunchMode, RuntimeLaunchProfile


def _profile(**overrides: object) -> AgentProfile:
    base: dict[str, object] = {
        "agent_id": "agent_def",
        "display_name": "Definition Agent",
        "roles": ("executor",),
        "trust_zone": AgentTrustZone.LOCAL_PRIVATE,
    }
    base.update(overrides)
    return AgentProfile(**base)  # type: ignore[arg-type]


def _runtime(runtime_id: str = "runtime_def", agent_id: str = "agent_def") -> AgentRuntime:
    return AgentRuntime(
        runtime_id=AgentRuntimeId(runtime_id),
        agent_id=AgentId(agent_id),
        adapter_kind=AgentAdapterKind.PROCESS,
        runtime_name="Definition process",
    )


def _launch_profile(runtime_id: str = "runtime_def", spec_sha256: str = "a" * 64) -> RuntimeLaunchProfile:
    return RuntimeLaunchProfile(
        runtime_id=AgentRuntimeId(runtime_id),
        launch_mode=LaunchMode.PROCESS,
        configuration={"argv": ["/usr/bin/sleep", "60"], "cwd": "/tmp"},
        spec_sha256=spec_sha256,
    )


def test_definition_digest_is_stable_and_content_sensitive() -> None:
    definition = AgentDefinition(
        profile=_profile(),
        runtimes=(_runtime(),),
        launch_profiles=(_launch_profile(),),
    )
    digest = definition.definition_sha256()
    assert len(digest) == 64

    rebuilt = AgentDefinition(
        profile=_profile(),
        runtimes=(_runtime(),),
        launch_profiles=(_launch_profile(),),
    )
    assert rebuilt.definition_sha256() == digest

    bumped = AgentDefinition(
        definition_version=2,
        profile=_profile(),
        runtimes=(_runtime(),),
        launch_profiles=(_launch_profile(),),
    )
    assert bumped.definition_sha256() != digest

    retagged = AgentDefinition(
        profile=_profile(),
        runtimes=(_runtime(),),
        launch_profiles=(_launch_profile(spec_sha256="b" * 64),),
    )
    assert retagged.definition_sha256() != digest


def test_definition_rejects_runtime_that_belongs_to_another_agent() -> None:
    with pytest.raises(ValidationError, match="belongs to another agent"):
        AgentDefinition(
            profile=_profile(),
            runtimes=(_runtime(agent_id="agent_other"),),
        )


def test_definition_rejects_launch_profile_for_unknown_runtime() -> None:
    with pytest.raises(ValidationError, match="unknown runtime"):
        AgentDefinition(
            profile=_profile(),
            runtimes=(_runtime(),),
            launch_profiles=(_launch_profile(runtime_id="runtime_missing"),),
        )


def test_profile_only_definition_is_valid() -> None:
    definition = AgentDefinition(profile=_profile())
    assert definition.runtimes == ()
    assert definition.launch_profiles == ()
    assert len(definition.definition_sha256()) == 64


def test_definition_reuses_existing_profile_model() -> None:
    definition = AgentDefinition(
        profile=_profile(),
        runtimes=(_runtime(),),
        launch_profiles=(_launch_profile(),),
    )
    assert isinstance(definition.profile, AgentProfile)
    assert isinstance(definition.runtimes[0], AgentRuntime)
    assert isinstance(definition.launch_profiles[0], RuntimeLaunchProfile)
    with pytest.raises(ValidationError):
        AgentDefinition(
            profile=_profile(),
            runtimes=(_runtime(),),
            launch_profiles=(_launch_profile(),),
            unexpected_field="value",  # type: ignore[call-arg]
        )
