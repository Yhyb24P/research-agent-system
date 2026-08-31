"""PX01-02: transactional AgentDefinition installation."""

from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from researchd.collaboration.agent_definitions import AgentDefinition
from researchd.collaboration.contracts import AgentProfile, AgentRuntime
from researchd.collaboration.install import AgentInstallService
from researchd.collaboration.registry import AgentRegistryService
from researchd.domain.enums import AgentAdapterKind, AgentTrustZone
from researchd.domain.ids import AgentId, AgentRuntimeId
from researchd.runtime_sessions.contracts import (
    LaunchMode,
    ProcessLaunchConfiguration,
    ProcessLaunchSpec,
    RemoteHttpLaunchConfiguration,
    RuntimeLaunchProfile,
)
from researchd.runtime_sessions.launch_profiles import RuntimeLaunchProfileService
from researchd.storage.db import create_sqlite_engine, session_factory
from tests.integration.test_storage import migrate


def _services(
    tmp_path: Path,
) -> tuple[sessionmaker[Session], AgentRegistryService, RuntimeLaunchProfileService, AgentInstallService]:
    database = tmp_path / "install.db"
    migrate(database)
    sessions = session_factory(create_sqlite_engine(database))
    registry = AgentRegistryService(sessions)
    launch_profiles = RuntimeLaunchProfileService(sessions, registry)
    return sessions, registry, launch_profiles, AgentInstallService(
        sessions, registry, launch_profiles,
    )


def _process_configuration() -> dict[str, object]:
    return ProcessLaunchConfiguration(
        launch_spec=ProcessLaunchSpec(argv=("/usr/bin/sleep", "60"), cwd="/tmp"),
    ).model_dump(mode="json")


def _launch_profile(
    runtime_id: str = "runtime_install",
    *,
    launch_mode: LaunchMode = LaunchMode.PROCESS,
    configuration: dict[str, object] | None = None,
    spec_sha256: str | None = None,
) -> RuntimeLaunchProfile:
    configuration = configuration if configuration is not None else _process_configuration()
    digest = RuntimeLaunchProfileService._digest(launch_mode, configuration)
    return RuntimeLaunchProfile(
        runtime_id=AgentRuntimeId(runtime_id),
        launch_mode=launch_mode,
        configuration=configuration,
        spec_sha256=spec_sha256 or digest,
    )


def _definition(
    *,
    agent_id: str = "agent_install",
    display_name: str = "Install Agent",
    runtimes: tuple[AgentRuntime, ...] = (),
    launch_profiles: tuple[RuntimeLaunchProfile, ...] = (),
    definition_version: int = 1,
) -> AgentDefinition:
    profile = AgentProfile(
        agent_id=AgentId(agent_id),
        display_name=display_name,
        roles=("executor",),
        trust_zone=AgentTrustZone.LOCAL_PRIVATE,
    )
    return AgentDefinition(
        definition_version=definition_version,
        profile=profile,
        runtimes=runtimes,
        launch_profiles=launch_profiles,
    )


def _process_runtime(agent_id: str = "agent_install") -> AgentRuntime:
    return AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_install"),
        agent_id=AgentId(agent_id),
        adapter_kind=AgentAdapterKind.PROCESS,
        runtime_name="Install process",
    )


def test_fresh_install_registers_profile_runtime_and_launch_profile(tmp_path: Path) -> None:
    sessions, registry, launch_profiles, installer = _services(tmp_path)

    result = installer.install(_definition(
        runtimes=(_process_runtime(),),
        launch_profiles=(_launch_profile(),),
    ))

    assert result.agent_id == AgentId("agent_install")
    assert result.definition_version == 1
    assert len(result.definition_sha256) == 64
    assert result.runtimes == ("runtime_install",)
    assert result.launch_profile_runtimes == ("runtime_install",)
    assert registry.get_agent("agent_install").display_name == "Install Agent"
    assert registry.get_runtime("runtime_install").runtime_name == "Install process"
    resolved = launch_profiles.resolve_process("runtime_install")
    assert resolved.launch_spec.argv == ("/usr/bin/sleep", "60")
    expected_digest = RuntimeLaunchProfileService._digest(
        LaunchMode.PROCESS, _process_configuration(),
    )
    assert resolved.spec_sha256 == expected_digest


def test_update_install_advances_versions_without_duplicates(tmp_path: Path) -> None:
    sessions, registry, launch_profiles, installer = _services(tmp_path)
    installer.install(_definition(
        runtimes=(_process_runtime(),),
        launch_profiles=(_launch_profile(),),
    ))

    installer.install(_definition(
        display_name="Renamed Agent",
        runtimes=(_process_runtime(),),
        launch_profiles=(_launch_profile(),),
        definition_version=2,
    ))

    profile = registry.get_agent("agent_install")
    assert profile.display_name == "Renamed Agent"
    assert profile.profile_version == 2
    assert len(registry.list_runtimes("agent_install")) == 1
    stored = launch_profiles.get("runtime_install")
    assert stored.version == 2


def test_install_rolls_back_atomically_on_owner_conflict(tmp_path: Path) -> None:
    sessions, registry, launch_profiles, installer = _services(tmp_path)
    other = AgentProfile(
        agent_id=AgentId("agent_other"),
        display_name="Other",
        roles=("executor",),
        trust_zone=AgentTrustZone.LOCAL_PRIVATE,
    )
    registry.register_profile(other)
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_shared"),
        agent_id=AgentId("agent_other"),
        adapter_kind=AgentAdapterKind.PROCESS,
        runtime_name="Shared process",
    ))

    with pytest.raises(ValueError, match="runtime owner cannot be changed"):
        installer.install(_definition(
            runtimes=(AgentRuntime(
                runtime_id=AgentRuntimeId("runtime_shared"),
                agent_id=AgentId("agent_install"),
                adapter_kind=AgentAdapterKind.PROCESS,
                runtime_name="Contested",
            ),),
        ))

    with pytest.raises(ValueError, match="does not exist"):
        registry.get_agent("agent_install")
    assert registry.get_runtime("runtime_shared").runtime_name == "Shared process"


def test_install_rejects_digest_mismatch_before_writing(tmp_path: Path) -> None:
    sessions, registry, launch_profiles, installer = _services(tmp_path)

    with pytest.raises(ValueError, match="digest does not match"):
        installer.install(_definition(
            runtimes=(_process_runtime(),),
            launch_profiles=(_launch_profile(spec_sha256="f" * 64),),
        ))

    with pytest.raises(ValueError, match="does not exist"):
        registry.get_agent("agent_install")


def test_install_rejects_adapter_incompatible_launch_profile(tmp_path: Path) -> None:
    sessions, registry, launch_profiles, installer = _services(tmp_path)
    http_runtime = AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_http"),
        agent_id=AgentId("agent_install"),
        adapter_kind=AgentAdapterKind.HTTP,
        runtime_name="Install http",
        endpoint_ref="https://agent.example.invalid",
    )

    with pytest.raises(ValueError, match="PROCESS launch profile requires a PROCESS"):
        installer.install(_definition(
            runtimes=(http_runtime,),
            launch_profiles=(_launch_profile(runtime_id="runtime_http"),),
        ))


def test_install_rejects_unsupported_launch_mode(tmp_path: Path) -> None:
    sessions, registry, launch_profiles, installer = _services(tmp_path)

    with pytest.raises(ValueError, match="not installable"):
        installer.install(_definition(
            runtimes=(_process_runtime(),),
            launch_profiles=(
                _launch_profile(launch_mode=LaunchMode.CLOUD, configuration={}),
            ),
        ))


def test_install_rejects_invalid_process_configuration(tmp_path: Path) -> None:
    sessions, registry, launch_profiles, installer = _services(tmp_path)

    with pytest.raises(ValueError):
        installer.install(_definition(
            runtimes=(_process_runtime(),),
            launch_profiles=(_launch_profile(configuration={"bogus": 1}),),
        ))


def test_remote_http_install_resolves_through_existing_service(tmp_path: Path) -> None:
    sessions, registry, launch_profiles, installer = _services(tmp_path)
    http_runtime = AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_http"),
        agent_id=AgentId("agent_install"),
        adapter_kind=AgentAdapterKind.HTTP,
        runtime_name="Install http",
        endpoint_ref="https://agent.example.invalid",
    )
    configuration = RemoteHttpLaunchConfiguration(health_path="/health").model_dump(mode="json")

    result = installer.install(_definition(
        runtimes=(http_runtime,),
        launch_profiles=(
            _launch_profile(
                runtime_id="runtime_http",
                launch_mode=LaunchMode.REMOTE_HTTP,
                configuration=configuration,
            ),
        ),
    ))
    assert result.launch_profile_runtimes == ("runtime_http",)
    resolved = launch_profiles.resolve_remote_http("runtime_http")
    assert resolved.launch_spec.endpoint == "https://agent.example.invalid"
    assert resolved.launch_spec.health_path == "/health"
