from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from researchd.api.control import LocalControlAPI
from researchd.api.web import ControlResourceRouter
from researchd.collaboration.contracts import AgentProfile, AgentRuntime
from researchd.collaboration.registry import AgentRegistryService
from researchd.domain.enums import AgentAdapterKind, AgentTrustZone
from researchd.domain.ids import AgentId, AgentRuntimeId, RuntimeSessionId
from researchd.runtime_sessions.contracts import (
    ExternalObservation,
    LaunchMode,
    ProcessLaunchSpec,
    RemoteHttpAttachSpec,
    RuntimeSessionAttachCommand,
    RuntimeSessionStartCommand,
    RuntimeSessionStopCommand,
    SupervisorState,
)
from researchd.runtime_sessions.service import (
    RuntimeSessionConflict,
    RuntimeSessionService,
)
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    AuditEventRecord,
    RuntimeSessionCommandRecord,
)
from researchd.supervisor.drivers import ManagedProcessDriver, RemoteHttpDriver
from researchd.supervisor.runtime import RuntimeLaunchError, RuntimeSupervisor
from tests.integration.test_storage import assert_migration_matches_models, migrate


ROOT = Path(__file__).parents[2]


class FakeProcessDriver:
    launch_mode = LaunchMode.PROCESS

    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.observation = ExternalObservation.PRESENT
        self.on_stop: Callable[[], None] | None = None

    def start(self, launch_spec: dict[str, object]) -> dict[str, object]:
        assert launch_spec["argv"] == ["/usr/bin/agent", "serve"]
        self.starts += 1
        return {"pid": 41, "start_ticks": 9001, "boot_id": "boot-test"}

    def observe(self, external_identity: dict[str, object]) -> ExternalObservation:
        assert external_identity["start_ticks"] == 9001
        return self.observation

    def stop(self, external_identity: dict[str, object]) -> ExternalObservation:
        assert external_identity["boot_id"] == "boot-test"
        self.stops += 1
        if self.on_stop is not None:
            self.on_stop()
        return ExternalObservation.ABSENT


class FailingStartDriver(FakeProcessDriver):
    def start(self, launch_spec: dict[str, object]) -> dict[str, object]:
        del launch_spec
        raise OSError("injected launch failure")


class FailingStopDriver(FakeProcessDriver):
    def stop(self, external_identity: dict[str, object]) -> ExternalObservation:
        del external_identity
        raise OSError("injected stop failure")


@pytest.fixture
def runtime_database(
    tmp_path: Path,
) -> tuple[Path, sessionmaker[Session], AgentRegistryService, RuntimeSessionService]:
    path = tmp_path / "runtime-sessions.db"
    migrate(path)
    assert_migration_matches_models(path)
    sessions = session_factory(create_sqlite_engine(path))
    registry = AgentRegistryService(sessions)
    registry.register_profile(AgentProfile(
        agent_id=AgentId("agent_executor"),
        display_name="Executor",
        roles=("executor",),
        skills=("code.modify",),
        trust_zone=AgentTrustZone.LOCAL_PRIVATE,
    ))
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_process"),
        agent_id=AgentId("agent_executor"),
        adapter_kind=AgentAdapterKind.PROCESS,
        runtime_name="Managed process",
    ))
    return path, sessions, registry, RuntimeSessionService(sessions, registry)


def start_command(
    *,
    command_id: str = "command_start_1",
    actor_id: str = "human-1",
) -> RuntimeSessionStartCommand:
    return RuntimeSessionStartCommand(
        command_id=command_id,
        runtime_session_id=RuntimeSessionId("runtime_session_process_1"),
        runtime_id=AgentRuntimeId("runtime_process"),
        actor_type="HUMAN",
        actor_id=actor_id,
        launch_spec=ProcessLaunchSpec(argv=("/usr/bin/agent", "serve"), cwd="/tmp"),
    )


def test_start_is_idempotent_and_emits_global_monotonic_events(
    runtime_database: tuple[
        Path,
        sessionmaker[Session],
        AgentRegistryService,
        RuntimeSessionService,
    ],
) -> None:
    _, sessions, _, service = runtime_database
    driver = FakeProcessDriver()
    supervisor = RuntimeSupervisor(service, (driver,))

    started = supervisor.start(start_command())
    replay = supervisor.start(start_command())

    assert started == replay
    assert started.supervisor_state is SupervisorState.HEALTHY
    assert started.version == 2
    assert started.external_identity == {
        "pid": 41,
        "start_ticks": 9001,
        "boot_id": "boot-test",
    }
    assert driver.starts == 1
    with sessions() as session:
        receipt = session.get(RuntimeSessionCommandRecord, "command_start_1")
        events = session.scalars(
            select(AuditEventRecord)
            .where(AuditEventRecord.run_id.is_(None))
            .order_by(AuditEventRecord.audit_seq)
        ).all()
        assert receipt is not None and receipt.status == "COMPLETED"
        assert [event.audit_seq for event in events] == [1, 2]
        assert [event.event_type for event in events] == [
            "RUNTIME_SESSION_START_REQUESTED",
            "RUNTIME_SESSION_HEALTHY",
        ]
        assert {event.actor_type for event in events} == {"HUMAN", "SYSTEM"}

    service.registry.set_runtime_enabled("runtime_process", False)
    assert supervisor.start(start_command()) == started
    assert driver.starts == 1

    with pytest.raises(RuntimeSessionConflict, match="different request"):
        supervisor.start(start_command(actor_id="human-2"))


def test_concurrent_start_commands_create_only_one_active_session(
    runtime_database: tuple[
        Path,
        sessionmaker[Session],
        AgentRegistryService,
        RuntimeSessionService,
    ],
) -> None:
    _, _, _, service = runtime_database
    driver = FakeProcessDriver()
    supervisor = RuntimeSupervisor(service, (driver,))

    with ThreadPoolExecutor(max_workers=2) as pool:
        same_results = tuple(pool.map(lambda _: supervisor.start(start_command()), range(2)))

    assert driver.starts == 1
    assert {item.runtime_session_id for item in same_results} == {
        RuntimeSessionId("runtime_session_process_1")
    }

    competing = start_command(
        command_id="command_start_competing",
    ).model_copy(update={
        "runtime_session_id": RuntimeSessionId("runtime_session_process_2"),
    })
    with pytest.raises(RuntimeSessionConflict, match="active RuntimeSession"):
        supervisor.start(competing)


def test_external_side_effect_failures_are_persisted_fail_closed(
    runtime_database: tuple[
        Path,
        sessionmaker[Session],
        AgentRegistryService,
        RuntimeSessionService,
    ],
) -> None:
    _, sessions, _, service = runtime_database
    with pytest.raises(RuntimeLaunchError, match="launch failed"):
        RuntimeSupervisor(service, (FailingStartDriver(),)).start(start_command())
    failed = service.get("runtime_session_process_1")
    assert failed.supervisor_state is SupervisorState.LOST
    assert failed.exit_reason == "launch_failed"
    with sessions() as session:
        receipt = session.get(RuntimeSessionCommandRecord, "command_start_1")
        assert receipt is not None and receipt.status == "FAILED"
        assert receipt.failure_reason == "OSError"


def test_stop_driver_failure_requires_reconciliation(
    runtime_database: tuple[
        Path,
        sessionmaker[Session],
        AgentRegistryService,
        RuntimeSessionService,
    ],
) -> None:
    _, sessions, _, service = runtime_database
    driver = FailingStopDriver()
    supervisor = RuntimeSupervisor(service, (driver,))
    started = supervisor.start(start_command())
    command = RuntimeSessionStopCommand(
        command_id="command_stop_failed",
        runtime_session_id=started.runtime_session_id,
        runtime_id=started.runtime_id,
        actor_type="SYSTEM",
        actor_id="operator",
        expected_version=started.version,
    )

    with pytest.raises(RuntimeLaunchError, match="stop failed"):
        supervisor.stop(command)

    failed = service.get(str(started.runtime_session_id))
    assert failed.supervisor_state is SupervisorState.RECONCILIATION_REQUIRED
    assert failed.exit_reason == "stop_failed"
    with sessions() as session:
        receipt = session.get(RuntimeSessionCommandRecord, "command_stop_failed")
        assert receipt is not None and receipt.status == "FAILED"


def test_stop_persists_intent_before_side_effect_and_replays_result(
    runtime_database: tuple[
        Path,
        sessionmaker[Session],
        AgentRegistryService,
        RuntimeSessionService,
    ],
) -> None:
    _, _, _, service = runtime_database
    driver = FakeProcessDriver()
    supervisor = RuntimeSupervisor(service, (driver,))
    started = supervisor.start(start_command())
    observed_states: list[SupervisorState] = []
    driver.on_stop = lambda: observed_states.append(
        service.get("runtime_session_process_1").supervisor_state
    )
    command = RuntimeSessionStopCommand(
        command_id="command_stop_1",
        runtime_session_id=RuntimeSessionId("runtime_session_process_1"),
        runtime_id=AgentRuntimeId("runtime_process"),
        actor_type="SYSTEM",
        actor_id="operator",
        expected_version=started.version,
    )

    stopped = supervisor.stop(command)
    replay = supervisor.stop(command)

    assert observed_states == [SupervisorState.STOPPING]
    assert stopped == replay
    assert stopped.supervisor_state is SupervisorState.STOPPED
    assert stopped.version == 4
    assert driver.stops == 1


def test_restart_reconciles_identity_without_relaunching(
    runtime_database: tuple[
        Path,
        sessionmaker[Session],
        AgentRegistryService,
        RuntimeSessionService,
    ],
) -> None:
    _, _, _, service = runtime_database
    driver = FakeProcessDriver()
    RuntimeSupervisor(service, (driver,)).start(start_command())
    driver.observation = ExternalObservation.ABSENT

    result = RuntimeSupervisor(service, (driver,)).reconcile_sessions()

    assert len(result) == 1
    assert result[0].supervisor_state is SupervisorState.LOST
    assert result[0].exit_reason == "external_instance_absent"
    assert driver.starts == 1


def test_crash_after_persisted_intent_requires_reconciliation_not_relaunch(
    runtime_database: tuple[
        Path,
        sessionmaker[Session],
        AgentRegistryService,
        RuntimeSessionService,
    ],
) -> None:
    _, _, _, service = runtime_database
    service.begin_start(start_command())
    driver = FakeProcessDriver()

    result = RuntimeSupervisor(service, (driver,)).reconcile_sessions()

    assert result[0].supervisor_state is SupervisorState.RECONCILIATION_REQUIRED
    assert result[0].exit_reason == "missing_external_identity"
    assert driver.starts == 0


def test_remote_attach_requires_registered_endpoint_and_typed_identity(
    runtime_database: tuple[
        Path,
        sessionmaker[Session],
        AgentRegistryService,
        RuntimeSessionService,
    ],
) -> None:
    _, _, registry, service = runtime_database
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_http"),
        agent_id=AgentId("agent_executor"),
        adapter_kind=AgentAdapterKind.HTTP,
        runtime_name="Remote HTTP",
        endpoint_ref="http://127.0.0.1:9080",
    ))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://127.0.0.1:9080/health"
        return httpx.Response(200, json={"runtime_instance_id": "remote-instance-1"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    driver = RemoteHttpDriver(client)
    command = RuntimeSessionAttachCommand(
        command_id="command_attach_1",
        runtime_session_id=RuntimeSessionId("runtime_session_http_1"),
        runtime_id=AgentRuntimeId("runtime_http"),
        actor_type="SYSTEM",
        actor_id="runtime-controller",
        launch_spec=RemoteHttpAttachSpec(endpoint="http://127.0.0.1:9080"),
    )

    attached = RuntimeSupervisor(service, (driver,)).attach(command)

    assert attached.supervisor_state is SupervisorState.HEALTHY
    assert attached.external_identity is not None
    assert attached.external_identity["instance_id"] == "remote-instance-1"
    with pytest.raises(ValueError, match="credentials"):
        RemoteHttpAttachSpec(endpoint="https://user:secret@example.test")


def test_runtime_session_projection_and_migration_objects(
    runtime_database: tuple[
        Path,
        sessionmaker[Session],
        AgentRegistryService,
        RuntimeSessionService,
    ],
) -> None:
    path, sessions, _, service = runtime_database
    RuntimeSupervisor(service, (FakeProcessDriver(),)).start(start_command())
    router = ControlResourceRouter(LocalControlAPI(sessions))

    status, sessions_payload = router.get("/api/runtime-sessions?runtime=runtime_process")
    event_status, event_payload = router.get("/api/system-events?after=0")

    assert status == 200 and isinstance(sessions_payload, list)
    assert sessions_payload[0]["supervisor_state"] == "HEALTHY"
    assert event_status == 200 and isinstance(event_payload, dict)
    assert len(event_payload["events"]) == 2
    engine = create_sqlite_engine(path)
    schema = inspect(engine)
    assert {"runtime_sessions", "runtime_session_commands"} <= set(schema.get_table_names())
    run_id_column = next(
        column for column in schema.get_columns("audit_events") if column["name"] == "run_id"
    )
    assert run_id_column["nullable"] is True
    assert {
        "ix_audit_events_run_seq",
        "ux_audit_events_audit_seq",
    } <= {index["name"] for index in schema.get_indexes("audit_events")}
    with engine.connect() as connection:
        assert connection.scalar(text(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'trigger' AND name = 'audit_events_assign_seq'"
        )) == 1


def test_current_0019_database_upgrades_to_runtime_session_contract(tmp_path: Path) -> None:
    path = tmp_path / "current-0019.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    alembic_command.upgrade(config, "0019")

    alembic_command.upgrade(config, "head")

    assert_migration_matches_models(path)
    engine = create_sqlite_engine(path)
    schema = inspect(engine)
    assert {"runtime_sessions", "runtime_session_commands"} <= set(schema.get_table_names())
    run_id_column = next(
        column for column in schema.get_columns("audit_events") if column["name"] == "run_id"
    )
    assert run_id_column["nullable"] is True
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0021"
        assert connection.scalar(text(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'trigger' AND name = 'audit_events_assign_seq'"
        )) == 1


def test_process_spec_rejects_relative_paths() -> None:
    with pytest.raises(ValueError, match="executable"):
        ProcessLaunchSpec(argv=("agent",), cwd="/tmp")
    with pytest.raises(ValueError, match="cwd"):
        ProcessLaunchSpec(argv=("/usr/bin/agent",), cwd="relative")


def test_managed_process_driver_rejects_pid_reuse_and_reaps_child(tmp_path: Path) -> None:
    driver = ManagedProcessDriver(stop_timeout_seconds=2)
    identity = driver.start(ProcessLaunchSpec(
        argv=("/usr/bin/sleep", "30"),
        cwd=str(tmp_path),
    ).model_dump(mode="json"))
    mismatched = dict(identity)
    start_ticks = identity["start_ticks"]
    assert isinstance(start_ticks, int) and not isinstance(start_ticks, bool)
    mismatched["start_ticks"] = start_ticks + 1

    try:
        assert driver.observe(identity) is ExternalObservation.PRESENT
        assert driver.observe(mismatched) is ExternalObservation.ABSENT
        assert driver.stop(identity) is ExternalObservation.ABSENT
        assert driver.observe(identity) is ExternalObservation.ABSENT
    finally:
        if driver.observe(identity) is ExternalObservation.PRESENT:
            driver.stop(identity)
