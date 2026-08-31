"""PH07 re-verification: supervised local runtime leases + policy capability config.

Targets the two fix commits on top of 4c2653a:

- 9e646a9 "fix: lease supervised local runtimes"
  - start acquires a daemon-owned lease, stop releases it,
  - a foreign lease holder makes reconciliation fail closed (no stealing),
  - daemon restart re-builds the lease for healthy PROCESS sessions,
  - RuntimeLeaseHeartbeat renews healthy local leases.
- d46eab1 "test: configure e2e policy capabilities"
  - DaemonConfig exposes workspace/user capability sets (default empty,
    fail-closed); the policy engine only allows the intersection.

Post-hoc test: no source changes, no commits.
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from researchd.collaboration.contracts import AgentProfile, AgentRuntime, AgentRuntimeLease
from researchd.collaboration.registry import AgentRegistryService
from researchd.daemon.composition import DaemonConfig
from researchd.domain.enums import (
    AgentAdapterKind,
    AgentTrustZone,
    Capability,
    DataClassification,
    PolicyOutcome,
)
from researchd.domain.ids import AgentId, AgentRuntimeId, RuntimeSessionId
from researchd.policy.engine import (
    BudgetLimits,
    DeterministicPolicyEngine,
    PolicyRequest,
)
from researchd.runtime_sessions.contracts import (
    ExternalObservation,
    LaunchMode,
    ProcessLaunchSpec,
    RuntimeSessionStartCommand,
    RuntimeSessionStopCommand,
    SupervisorState,
)
from researchd.runtime_sessions.service import RuntimeSessionService
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import AgentRuntimeRecord
from researchd.supervisor.runtime import (
    RuntimeLeaseHeartbeat,
    RuntimeLaunchError,
    RuntimeSupervisor,
)
from tests.integration.test_storage import assert_migration_matches_models, migrate

RUNTIME_ID = "runtime_process"
SESSION_ID = "runtime_session_ph07_1"
SESSION_OWNER = f"runtime-session:{SESSION_ID}"

ZERO_BUDGET = BudgetLimits(0, 0, 0, 0, 0)
FULL_BUDGET = BudgetLimits(3600, 3600, 3600, 1024, 1024)


class FakeProcessDriver:
    """PROCESS driver with controllable observation, mirroring test_runtime_sessions."""

    launch_mode = LaunchMode.PROCESS

    def __init__(self) -> None:
        self.starts = 0
        self.observation = ExternalObservation.PRESENT

    def start(self, launch_spec: dict[str, object]) -> dict[str, object]:
        self.starts += 1
        return {"pid": 41, "start_ticks": 9001, "boot_id": "boot-ph07"}

    def observe(self, external_identity: dict[str, object]) -> ExternalObservation:
        return self.observation

    def stop(self, external_identity: dict[str, object]) -> ExternalObservation:
        return ExternalObservation.ABSENT


class FailingStartDriver(FakeProcessDriver):
    def start(self, launch_spec: dict[str, object]) -> dict[str, object]:
        raise OSError("injected launch failure")


class Fixture:
    def __init__(self, tmp_path: Path) -> None:
        path = tmp_path / "ph07-lease.db"
        migrate(path)
        assert_migration_matches_models(path)
        self.sessions = session_factory(create_sqlite_engine(path))
        self.registry = AgentRegistryService(self.sessions)
        self.registry.register_profile(AgentProfile(
            agent_id=AgentId("agent_executor"),
            display_name="Executor",
            roles=("executor",),
            skills=("code.modify",),
            trust_zone=AgentTrustZone.LOCAL_PRIVATE,
        ))
        self.registry.register_runtime(AgentRuntime(
            runtime_id=AgentRuntimeId(RUNTIME_ID),
            agent_id=AgentId("agent_executor"),
            adapter_kind=AgentAdapterKind.PROCESS,
            runtime_name="Managed process",
        ))
        self.service = RuntimeSessionService(self.sessions, self.registry)

    def lease_row(self) -> AgentRuntimeRecord:
        with self.sessions() as session:
            row = session.get(AgentRuntimeRecord, RUNTIME_ID)
            assert row is not None
            session.expunge(row)
            return row

    def start_command(self, command_id: str = "cmd_ph07_start") -> RuntimeSessionStartCommand:
        return RuntimeSessionStartCommand(
            command_id=command_id,
            runtime_session_id=RuntimeSessionId(SESSION_ID),
            runtime_id=AgentRuntimeId(RUNTIME_ID),
            actor_type="HUMAN",
            actor_id="human-ph07",
            launch_spec=ProcessLaunchSpec(argv=("/usr/bin/agent", "serve"), cwd="/tmp"),
        )


@pytest.fixture
def fixture(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


# ----------------------------------------------------------------------
# 1. Lease: start acquires, stop releases, conflict fails closed,
#    restart rebuilds, heartbeat renews.
# ----------------------------------------------------------------------

def test_start_acquires_local_lease(fixture: Fixture) -> None:
    driver = FakeProcessDriver()
    supervisor = RuntimeSupervisor(fixture.service, (driver,), registry=fixture.registry)
    session = supervisor.start(fixture.start_command())
    assert session.supervisor_state is SupervisorState.HEALTHY

    row = fixture.lease_row()
    assert row.runtime_lease_id is not None
    assert row.lease_owner_id == SESSION_OWNER
    assert row.lease_expires_at is not None and row.lease_expires_at > datetime.now(UTC)


def test_failed_start_releases_lease(fixture: Fixture) -> None:
    supervisor = RuntimeSupervisor(fixture.service, (FailingStartDriver(),), registry=fixture.registry)
    with pytest.raises(RuntimeLaunchError):
        supervisor.start(fixture.start_command())
    row = fixture.lease_row()
    assert row.runtime_lease_id is None
    assert row.lease_owner_id is None
    assert row.lease_expires_at is None


def test_stop_releases_own_lease(fixture: Fixture) -> None:
    supervisor = RuntimeSupervisor(fixture.service, (FakeProcessDriver(),), registry=fixture.registry)
    session = supervisor.start(fixture.start_command())
    row = fixture.lease_row()
    assert row.runtime_lease_id is not None

    stopped = supervisor.stop(RuntimeSessionStopCommand(
        command_id="cmd_ph07_stop",
        runtime_session_id=session.runtime_session_id,
        runtime_id=session.runtime_id,
        actor_type="HUMAN",
        actor_id="human-ph07",
        expected_version=session.version,
    ))
    assert stopped.supervisor_state is SupervisorState.STOPPED
    assert fixture.lease_row().runtime_lease_id is None


def test_reconcile_conflict_with_foreign_lease_fails_closed(fixture: Fixture) -> None:
    supervisor = RuntimeSupervisor(fixture.service, (FakeProcessDriver(),), registry=fixture.registry)
    session = supervisor.start(fixture.start_command())
    held = fixture.lease_row()
    assert held.runtime_lease_id is not None
    assert held.lease_owner_id is not None
    assert held.lease_acquired_at is not None
    assert held.lease_expires_at is not None
    fixture.registry.release_runtime(AgentRuntimeLease(
        lease_id=held.runtime_lease_id,
        runtime_id=AgentRuntimeId(RUNTIME_ID),
        owner_id=held.lease_owner_id,
        acquired_at=held.lease_acquired_at,
        expires_at=held.lease_expires_at,
    ))
    foreign = fixture.registry.acquire_runtime(RUNTIME_ID, owner_id="remote-other")

    # Simulated daemon restart: a fresh supervisor reconciles the surviving
    # session. The foreign owner must not be displaced.
    restarted = RuntimeSupervisor(fixture.service, (FakeProcessDriver(),), registry=fixture.registry)
    (reconciled,) = restarted.reconcile_sessions()
    assert reconciled.supervisor_state is SupervisorState.RECONCILIATION_REQUIRED

    row = fixture.lease_row()
    assert row.runtime_lease_id == foreign.lease_id
    assert row.lease_owner_id == "remote-other"


def test_restart_rebuilds_lease_for_healthy_session(fixture: Fixture) -> None:
    supervisor = RuntimeSupervisor(fixture.service, (FakeProcessDriver(),), registry=fixture.registry)
    supervisor.start(fixture.start_command())
    before = fixture.lease_row()
    assert before.runtime_lease_id is not None

    # Simulated daemon restart: fresh supervisor instance, same session row.
    restarted = RuntimeSupervisor(fixture.service, (FakeProcessDriver(),), registry=fixture.registry)
    (reconciled,) = restarted.reconcile_sessions()
    assert reconciled.supervisor_state is SupervisorState.HEALTHY
    assert reconciled.reattach_state.name == "ATTACHED"

    after = fixture.lease_row()
    assert after.runtime_lease_id is not None
    assert after.lease_owner_id == SESSION_OWNER
    assert after.lease_expires_at is not None
    assert before.lease_expires_at is not None
    assert after.lease_expires_at > before.lease_expires_at


def test_heartbeat_renews_healthy_process_leases(fixture: Fixture) -> None:
    driver = FakeProcessDriver()
    supervisor = RuntimeSupervisor(fixture.service, (driver,), registry=fixture.registry, lease_seconds=1)
    supervisor.start(fixture.start_command())
    before = fixture.lease_row().lease_expires_at
    assert before is not None

    heartbeat = RuntimeLeaseHeartbeat(supervisor, interval_seconds=0.1)
    heartbeat.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            current = fixture.lease_row().lease_expires_at
            assert current is not None
            if current > before:
                break
            time.sleep(0.05)
    finally:
        heartbeat.stop()

    renewed = fixture.lease_row().lease_expires_at
    assert renewed is not None and renewed > before
    health = heartbeat.health()
    assert health["last_error"] is None
    assert health["last_renewed"] == 1


def test_without_registry_no_lease_is_taken(fixture: Fixture) -> None:
    supervisor = RuntimeSupervisor(fixture.service, (FakeProcessDriver(),))
    supervisor.start(fixture.start_command())
    row = fixture.lease_row()
    assert row.runtime_lease_id is None
    assert row.lease_owner_id is None


# ----------------------------------------------------------------------
# 2. Policy configuration: default empty sets deny, intersection allows.
# ----------------------------------------------------------------------

def _request(
    *,
    requested: frozenset[Capability],
    workspace: frozenset[Capability],
    user: frozenset[Capability],
) -> PolicyRequest:
    return PolicyRequest(
        requested_capabilities=requested,
        workspace_capabilities=workspace,
        user_capabilities=user,
        approved_capabilities=frozenset(),
        requested_budget=ZERO_BUDGET,
        maximum_budget=FULL_BUDGET,
        data_classification=DataClassification.PUBLIC,
    )


def test_policy_default_empty_sets_deny() -> None:
    decision = DeterministicPolicyEngine().evaluate(_request(
        requested=frozenset({Capability.SANDBOX_SHELL}),
        workspace=frozenset(),
        user=frozenset(),
    ))
    assert decision.outcome is PolicyOutcome.DENY
    assert "POLICY_DENY_CAPABILITY" in decision.reason_codes
    assert decision.effective_capabilities == ()


def test_policy_both_sides_authorization_allows() -> None:
    decision = DeterministicPolicyEngine().evaluate(_request(
        requested=frozenset({Capability.SANDBOX_SHELL}),
        workspace=frozenset({Capability.SANDBOX_SHELL}),
        user=frozenset({Capability.SANDBOX_SHELL}),
    ))
    assert decision.outcome is PolicyOutcome.ALLOW
    assert decision.effective_capabilities == (Capability.SANDBOX_SHELL,)
    assert decision.reason_codes == ()


def test_policy_single_side_authorization_denies() -> None:
    for workspace, user in (
        (frozenset({Capability.SANDBOX_SHELL}), frozenset()),
        (frozenset(), frozenset({Capability.SANDBOX_SHELL})),
    ):
        decision = DeterministicPolicyEngine().evaluate(_request(
            requested=frozenset({Capability.SANDBOX_SHELL}),
            workspace=workspace,
            user=user,
        ))
        assert decision.outcome is PolicyOutcome.DENY
        assert "POLICY_DENY_CAPABILITY" in decision.reason_codes


def test_daemon_config_parses_and_defaults_capabilities(tmp_path: Path) -> None:
    defaults = DaemonConfig(
        database=tmp_path / "db.sqlite",
        artifact_root=tmp_path / "artifacts",
        state_root=tmp_path / "state",
    )
    assert defaults.workspace_capabilities == frozenset()
    assert defaults.user_capabilities == frozenset()

    # The product loads operator config as JSON (cli: DaemonConfig.model_validate_json),
    # where JSON arrays coerce to frozenset[Capability] — the path the E2E harness uses.
    explicit = DaemonConfig.model_validate_json(json.dumps({
        "database": str(tmp_path / "db.sqlite"),
        "artifact_root": str(tmp_path / "artifacts"),
        "state_root": str(tmp_path / "state"),
        "workspace_capabilities": ["sandbox.shell"],
        "user_capabilities": ["sandbox.shell"],
    }))
    assert explicit.workspace_capabilities == frozenset({Capability.SANDBOX_SHELL})
    assert explicit.user_capabilities == frozenset({Capability.SANDBOX_SHELL})
