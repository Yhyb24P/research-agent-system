"""PX01-03: agent-scoped ManagedAgentStart resolution and dispatch."""

import asyncio
import inspect
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session, sessionmaker

from researchd.collaboration.contracts import AgentProfile, AgentRuntime
from researchd.collaboration.registry import AgentRegistryService
from researchd.daemon.contracts import (
    DaemonCommandResult,
    ManagedAgentStartCommand,
)
from researchd.daemon.dispatcher import DaemonCommandDispatcher
from researchd.domain.base import DomainModel
from researchd.domain.enums import AgentAdapterKind, AgentTrustZone
from researchd.domain.ids import AgentId, AgentRuntimeId
from researchd.runtime_sessions.contracts import (
    ProcessLaunchSpec,
    RemoteHttpLaunchConfiguration,
    RuntimeSessionAttachCommand,
    RuntimeSessionStartCommand,
)
from researchd.runtime_sessions.launch_profiles import RuntimeLaunchProfileService
from researchd.runtime_sessions.managed_start import (
    ManagedAgentStartService,
    _session_id_for,
)
from researchd.runtime_sessions.service import RuntimeSessionService
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.supervisor.runtime import RuntimeSupervisor
from tests.integration.test_daemon import _composed_application
from tests.integration.test_storage import migrate


def _services(
    tmp_path: Path,
) -> tuple[AgentRegistryService, RuntimeLaunchProfileService, ManagedAgentStartService]:
    database = tmp_path / "managed.db"
    migrate(database)
    sessions = session_factory(create_sqlite_engine(database))
    registry = AgentRegistryService(sessions)
    launch_profiles = RuntimeLaunchProfileService(sessions, registry)
    return registry, launch_profiles, ManagedAgentStartService(registry, launch_profiles)


def _register_process_agent(
    registry: AgentRegistryService,
    launch_profiles: RuntimeLaunchProfileService,
    agent_id: str = "agent_managed",
    runtime_id: str = "runtime_managed",
) -> None:
    registry.register_profile(AgentProfile(
        agent_id=AgentId(agent_id),
        display_name="Managed Agent",
        roles=("executor",),
        trust_zone=AgentTrustZone.LOCAL_PRIVATE,
    ))
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId(runtime_id),
        agent_id=AgentId(agent_id),
        adapter_kind=AgentAdapterKind.PROCESS,
        runtime_name="Managed process",
    ))
    launch_profiles.register_process(
        runtime_id,
        ProcessLaunchSpec(argv=("/usr/bin/true",), cwd="/tmp"),
    )


def _register_http_agent(
    registry: AgentRegistryService,
    launch_profiles: RuntimeLaunchProfileService,
    agent_id: str = "agent_http",
    runtime_id: str = "runtime_http",
) -> None:
    registry.register_profile(AgentProfile(
        agent_id=AgentId(agent_id),
        display_name="HTTP Agent",
        roles=("executor",),
        trust_zone=AgentTrustZone.LOCAL_PRIVATE,
    ))
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId(runtime_id),
        agent_id=AgentId(agent_id),
        adapter_kind=AgentAdapterKind.HTTP,
        runtime_name="Managed HTTP",
        endpoint_ref="https://runtime.local:8443",
    ))
    launch_profiles.register_remote_http(runtime_id, RemoteHttpLaunchConfiguration())


def test_single_enabled_runtime_is_selected_without_runtime_id(
    tmp_path: Path,
) -> None:
    registry, launch_profiles, service = _services(tmp_path)
    _register_process_agent(registry, launch_profiles)

    command = service.resolve(
        "agent_managed", None,
        command_id="cmd_managed_1",
        actor_type="HUMAN",
        actor_id="operator",
    )

    assert isinstance(command, RuntimeSessionStartCommand)
    assert command.runtime_id == AgentRuntimeId("runtime_managed")
    assert command.runtime_session_id == _session_id_for("cmd_managed_1")
    assert command.launch_spec.argv == ("/usr/bin/true",)
    assert command.launch_profile_sha256 == launch_profiles.get("runtime_managed").spec_sha256
    assert command.actor_type == "HUMAN"
    assert command.actor_id == "operator"


def test_explicit_runtime_id_selects_that_runtime(tmp_path: Path) -> None:
    registry, launch_profiles, service = _services(tmp_path)
    _register_process_agent(registry, launch_profiles)
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_second"),
        agent_id=AgentId("agent_managed"),
        adapter_kind=AgentAdapterKind.PROCESS,
        runtime_name="Second process",
    ))
    launch_profiles.register_process(
        "runtime_second",
        ProcessLaunchSpec(argv=("/usr/bin/true",), cwd="/tmp"),
    )

    command = service.resolve(
        "agent_managed", "runtime_second",
        command_id="cmd_managed_2",
        actor_type="HUMAN",
        actor_id="operator",
    )

    assert isinstance(command, RuntimeSessionStartCommand)
    assert command.runtime_id == AgentRuntimeId("runtime_second")


def test_runtime_owned_by_other_agent_is_rejected(tmp_path: Path) -> None:
    registry, launch_profiles, service = _services(tmp_path)
    _register_process_agent(registry, launch_profiles)
    _register_process_agent(
        registry, launch_profiles,
        agent_id="agent_other", runtime_id="runtime_other",
    )

    with pytest.raises(ValueError, match="does not belong to agent"):
        service.resolve(
            "agent_managed", "runtime_other",
            command_id="cmd_foreign",
            actor_type="HUMAN",
            actor_id="operator",
        )


def test_disabled_agent_is_rejected(tmp_path: Path) -> None:
    registry, launch_profiles, service = _services(tmp_path)
    _register_process_agent(registry, launch_profiles)
    registry.disable("agent_managed")

    with pytest.raises(ValueError, match="agent is disabled"):
        service.resolve(
            "agent_managed", "runtime_managed",
            command_id="cmd_disabled",
            actor_type="HUMAN",
            actor_id="operator",
        )


def test_ambiguous_runtime_selection_is_rejected(tmp_path: Path) -> None:
    registry, launch_profiles, service = _services(tmp_path)
    _register_process_agent(registry, launch_profiles)
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_second"),
        agent_id=AgentId("agent_managed"),
        adapter_kind=AgentAdapterKind.PROCESS,
        runtime_name="Second process",
    ))
    launch_profiles.register_process(
        "runtime_second",
        ProcessLaunchSpec(argv=("/usr/bin/true",), cwd="/tmp"),
    )

    with pytest.raises(ValueError, match="runtime_id is required"):
        service.resolve(
            "agent_managed", None,
            command_id="cmd_ambiguous",
            actor_type="HUMAN",
            actor_id="operator",
        )


def test_unsupported_adapter_kind_is_rejected(tmp_path: Path) -> None:
    registry, launch_profiles, service = _services(tmp_path)
    registry.register_profile(AgentProfile(
        agent_id=AgentId("agent_internal"),
        display_name="Internal Agent",
        roles=("executor",),
        trust_zone=AgentTrustZone.LOCAL_PRIVATE,
    ))
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_internal"),
        agent_id=AgentId("agent_internal"),
        adapter_kind=AgentAdapterKind.INTERNAL,
        runtime_name="Internal runtime",
    ))

    with pytest.raises(ValueError, match="not supported for adapter"):
        service.resolve(
            "agent_internal", "runtime_internal",
            command_id="cmd_internal",
            actor_type="HUMAN",
            actor_id="operator",
        )


def test_missing_launch_profile_is_rejected(tmp_path: Path) -> None:
    registry, launch_profiles, service = _services(tmp_path)
    registry.register_profile(AgentProfile(
        agent_id=AgentId("agent_bare"),
        display_name="Bare Agent",
        roles=("executor",),
        trust_zone=AgentTrustZone.LOCAL_PRIVATE,
    ))
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_bare"),
        agent_id=AgentId("agent_bare"),
        adapter_kind=AgentAdapterKind.PROCESS,
        runtime_name="Bare process",
    ))

    with pytest.raises(ValueError, match="launch profile does not exist"):
        service.resolve(
            "agent_bare", "runtime_bare",
            command_id="cmd_bare",
            actor_type="HUMAN",
            actor_id="operator",
        )


def test_http_runtime_resolves_to_attach_command(tmp_path: Path) -> None:
    registry, launch_profiles, service = _services(tmp_path)
    _register_http_agent(registry, launch_profiles)

    command = service.resolve(
        "agent_http", None,
        command_id="cmd_http",
        actor_type="HUMAN",
        actor_id="operator",
    )

    assert isinstance(command, RuntimeSessionAttachCommand)
    assert command.launch_spec.endpoint == "https://runtime.local:8443"
    assert command.launch_profile_sha256 == launch_profiles.get("runtime_http").spec_sha256


def test_session_identity_is_derived_from_command_id(tmp_path: Path) -> None:
    registry, launch_profiles, service = _services(tmp_path)
    _register_process_agent(registry, launch_profiles)

    first = service.resolve(
        "agent_managed", None,
        command_id="cmd_same",
        actor_type="HUMAN",
        actor_id="operator",
    )
    replay = service.resolve(
        "agent_managed", None,
        command_id="cmd_same",
        actor_type="HUMAN",
        actor_id="operator",
    )
    other = service.resolve(
        "agent_managed", None,
        command_id="cmd_other",
        actor_type="HUMAN",
        actor_id="operator",
    )

    assert first.runtime_session_id == replay.runtime_session_id
    assert first.runtime_session_id != other.runtime_session_id
    assert str(first.runtime_session_id).startswith("runtime_session_managed_")


def test_dispatcher_routes_managed_start_through_supervisor(tmp_path: Path) -> None:
    database = tmp_path / "dispatch.db"
    migrate(database)
    sessions = session_factory(create_sqlite_engine(database))
    registry = AgentRegistryService(sessions)
    launch_profiles = RuntimeLaunchProfileService(sessions, registry)
    supervisor = RuntimeSupervisor(RuntimeSessionService(sessions, registry))
    service = ManagedAgentStartService(registry, launch_profiles)
    _register_process_agent(registry, launch_profiles)
    dispatcher = DaemonCommandDispatcher(supervisor, managed_start=service)

    result = dispatcher(ManagedAgentStartCommand(
        command_id="cmd_dispatch_managed",
        actor_type="HUMAN",
        actor_id="operator",
        agent_id="agent_managed",
    ))
    assert isinstance(result, DaemonCommandResult)
    assert result.command_type == "ManagedAgentStart"
    assert result.status == "ACCEPTED"
    assert result.resource is not None
    assert result.resource["supervisor_state"] == "HEALTHY"
    assert result.resource["runtime_session_id"] == str(_session_id_for("cmd_dispatch_managed"))
    assert result.resource["launch_spec"] == {"argv": ["/usr/bin/true"], "cwd": "/tmp"}


def test_dispatcher_fails_closed_without_managed_start_authority(tmp_path: Path) -> None:
    database = tmp_path / "closed.db"
    migrate(database)
    sessions = session_factory(create_sqlite_engine(database))
    registry = AgentRegistryService(sessions)
    supervisor = RuntimeSupervisor(RuntimeSessionService(sessions, registry))
    dispatcher = DaemonCommandDispatcher(supervisor)

    with pytest.raises(RuntimeError, match="managed agent start authority is not configured"):
        dispatcher(ManagedAgentStartCommand(
            command_id="cmd_no_authority",
            actor_type="HUMAN",
            actor_id="operator",
            agent_id="agent_managed",
        ))


def test_composed_daemon_managed_start_is_durable_and_idempotent(tmp_path: Path) -> None:
    application = _composed_application(tmp_path)
    sessions = session_factory(create_sqlite_engine(application.config.database))
    registry = AgentRegistryService(sessions)
    launch_profiles = RuntimeLaunchProfileService(sessions, registry)
    _register_process_agent(registry, launch_profiles)

    command = ManagedAgentStartCommand(
        command_id="cmd_composed_managed",
        actor_type="HUMAN",
        actor_id="operator",
        agent_id="agent_managed",
    )

    def execute(managed: ManagedAgentStartCommand) -> DaemonCommandResult:
        outcome = application.daemon.execute(managed)
        if inspect.isawaitable(outcome):
            outcome = asyncio.run(cast("Coroutine[Any, Any, DomainModel]", outcome))
        assert isinstance(outcome, DaemonCommandResult)
        return outcome

    result = execute(command)
    assert result.command_type == "ManagedAgentStart"
    assert result.status == "ACCEPTED"
    assert result.resource is not None
    assert result.resource["supervisor_state"] == "HEALTHY"
    assert result.resource["runtime_session_id"] == str(_session_id_for("cmd_composed_managed"))
    assert result.resource["launch_spec"] == {"argv": ["/usr/bin/true"], "cwd": "/tmp"}

    assert execute(command) == result
