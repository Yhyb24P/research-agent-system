from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker

from researchd.collaboration.contracts import (
    AgentInvocationRequest,
    AgentInvocationResult,
    AgentProfile,
    AgentRuntime,
    AgentRuntimeLease,
    PlanInvocationInput,
)
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.heterogeneous import HttpAgentAdapter, LocalProcessAgentAdapter
from researchd.collaboration.invocation import InvocationService, StaleInvocationResult
from researchd.collaboration.registry import AgentRegistryService, RuntimeLeaseConflict
from researchd.collaboration.runtime import AgentAdapterCatalog
from researchd.collaboration.selector import AgentSelector
from researchd.context.builder import CloudContextSelection
from researchd.domain.enums import (
    AgentAdapterKind,
    AgentTrustZone,
    DelegationPurpose,
    InvocationStatus,
    ResearchRunState,
)
from researchd.domain.ids import AgentId, AgentRuntimeId, DelegationId, InvocationId
from researchd.observability import collect_metrics
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    AgentInvocationRecord,
    AgentRuntimeLeaseEventRecord,
    ArtifactRecord,
    AuditEventRecord,
    DelegationRecord,
    ResearchRunRecord,
    VerificationResultRecord,
    WorkspaceRecord,
)


ROOT = Path(__file__).parents[2]
RUN_ID = "run_dq02"
PRIMARY_AGENT = AgentId("agent_dq02_primary")
PRIMARY_RUNTIME = AgentRuntimeId("runtime_dq02_primary")


def _database(path: Path) -> sessionmaker[Session]:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")
    sessions = session_factory(create_sqlite_engine(path))
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(WorkspaceRecord(
            workspace_id="ws_dq02",
            name="DQ02",
            version=1,
            created_at=now,
            updated_at=now,
        ))
        session.flush()
        session.add(ResearchRunRecord(
            run_id=RUN_ID,
            workspace_id="ws_dq02",
            objective="qualify Agent runtime lifecycle",
            state=ResearchRunState.ACTIVE.value,
            version=1,
            created_at=now,
            updated_at=now,
        ))
    return sessions


def _register(
    sessions: sessionmaker[Session],
) -> tuple[AgentRegistryService, AgentRuntimeLease]:
    registry = AgentRegistryService(sessions)
    registry.register_profile(AgentProfile(
        agent_id=PRIMARY_AGENT,
        display_name="DQ02 primary Agent",
        roles=("planner",),
        skills=("qualification.lifecycle",),
        trust_zone=AgentTrustZone.LOCAL_PRIVATE,
        max_parallel_delegations=1,
    ))
    registry.register_runtime(AgentRuntime(
        runtime_id=PRIMARY_RUNTIME,
        agent_id=PRIMARY_AGENT,
        adapter_kind=AgentAdapterKind.HTTP,
        runtime_name="DQ02 primary runtime",
        endpoint_ref="http://127.0.0.1:8765/agent",
    ))
    registry.register_profile(AgentProfile(
        agent_id=AgentId("agent_dq02_untrusted"),
        display_name="DQ02 unauthorized fallback",
        roles=("planner",),
        skills=("qualification.lifecycle",),
        trust_zone=AgentTrustZone.EXTERNAL_UNTRUSTED,
    ))
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_dq02_untrusted"),
        agent_id=AgentId("agent_dq02_untrusted"),
        adapter_kind=AgentAdapterKind.HTTP,
        runtime_name="DQ02 unauthorized fallback runtime",
    ))
    registry.acquire_runtime(
        "runtime_dq02_untrusted", owner_id="untrusted-instance", lease_seconds=300
    )
    registry.register_profile(AgentProfile(
        agent_id=AgentId("agent_dq02_contended"),
        display_name="DQ02 contended Agent",
        trust_zone=AgentTrustZone.LOCAL_PRIVATE,
    ))
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_dq02_contended"),
        agent_id=AgentId("agent_dq02_contended"),
        adapter_kind=AgentAdapterKind.PROCESS,
        runtime_name="DQ02 contended runtime",
    ))
    primary_lease = registry.acquire_runtime(
        str(PRIMARY_RUNTIME), owner_id="primary-instance-a", lease_seconds=300
    )
    return registry, primary_lease


def _request(
    sessions: sessionmaker[Session],
    *,
    ordinal: int,
) -> AgentInvocationRequest:
    delegation_id = DelegationId(f"del_dq02_{ordinal:02d}")
    invocation_id = InvocationId(f"inv_dq02_{ordinal:02d}")
    delegations = DelegationService(sessions)
    from researchd.collaboration.contracts import Delegation

    delegations.create(Delegation(
        delegation_id=delegation_id,
        run_id=RUN_ID,
        purpose=DelegationPurpose.PLAN,
        required_roles=("planner",),
        required_trust_zones=(AgentTrustZone.LOCAL_PRIVATE,),
        idempotency_key=f"dq02-{ordinal:02d}",
    ))
    delegations.assign(
        str(delegation_id),
        agent_id=str(PRIMARY_AGENT),
        runtime_id=str(PRIMARY_RUNTIME),
    )
    return AgentInvocationRequest(
        invocation_id=invocation_id,
        delegation_id=delegation_id,
        run_id=RUN_ID,
        agent_id=PRIMARY_AGENT,
        runtime_id=PRIMARY_RUNTIME,
        purpose=DelegationPurpose.PLAN,
        input_sha256=f"{ordinal:064x}",
        endpoint_ref="http://127.0.0.1:8765/agent",
        typed_input=PlanInvocationInput(context=CloudContextSelection(run_id=RUN_ID)),
    )


def test_dq02_runtime_and_invocation_lifecycle_matrix(tmp_path: Path) -> None:
    sessions = _database(tmp_path / "dq02.db")
    registry, primary_lease = _register(sessions)
    invocations = InvocationService(sessions)

    explicitly_renewed = registry.renew_runtime(primary_lease, lease_seconds=300)
    assert explicitly_renewed.lease_id == primary_lease.lease_id
    renewed = registry.acquire_runtime(
        str(PRIMARY_RUNTIME), owner_id="primary-instance-a", lease_seconds=300
    )
    assert renewed.lease_id == primary_lease.lease_id
    with pytest.raises(RuntimeLeaseConflict):
        registry.acquire_runtime(
            str(PRIMARY_RUNTIME), owner_id="primary-instance-b", lease_seconds=300
        )

    barrier = threading.Barrier(2)
    acquired: list[str] = []
    conflicts: list[str] = []

    def contend(owner_id: str) -> None:
        barrier.wait(timeout=2)
        try:
            lease = registry.acquire_runtime(
                "runtime_dq02_contended", owner_id=owner_id, lease_seconds=300
            )
            acquired.append(lease.lease_id)
        except RuntimeLeaseConflict:
            conflicts.append(owner_id)

    threads = [
        threading.Thread(target=contend, args=(owner,), daemon=True)
        for owner in ("contender-a", "contender-b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert len(acquired) == 1 and len(conflicts) == 1

    restart_request = _request(sessions, ordinal=1)
    invocations.start(restart_request)
    invocations.start(restart_request)
    invocations.mark_dispatched(
        str(restart_request.invocation_id), external_invocation_id="external-restart-1"
    )
    assert AgentSelector(sessions).select(
        required_roles=("planner",),
        required_trust_zones=(AgentTrustZone.LOCAL_PRIVATE,),
    ) is None
    assert invocations.recover_run(RUN_ID) == (str(restart_request.invocation_id),)
    with sessions() as session:
        row = session.get(AgentInvocationRecord, str(restart_request.invocation_id))
        assert row is not None
        assert row.status == InvocationStatus.RUNNING.value
        assert row.reason_code == "RECONCILIATION_REQUIRED"
        assert row.external_invocation_id == "external-restart-1"
        assert session.query(AgentInvocationRecord).count() == 1

    registry.release_runtime(primary_lease)
    restarted_lease = registry.acquire_runtime(
        str(PRIMARY_RUNTIME), owner_id="primary-instance-b", lease_seconds=300
    )
    assert restarted_lease.lease_id != primary_lease.lease_id
    assert collect_metrics(sessions).agent_invocation_orphans == 1
    with pytest.raises(StaleInvocationResult, match="stale"):
        invocations.complete(AgentInvocationResult(
            invocation_id=restart_request.invocation_id,
            external_invocation_id="external-stale",
            status=InvocationStatus.SUCCEEDED,
        ))
    invocations.complete(AgentInvocationResult(
        invocation_id=restart_request.invocation_id,
        external_invocation_id="external-restart-1",
        status=InvocationStatus.SUCCEEDED,
        output_type="PlanProposal",
        output={"proposal_id": "qualified"},
    ))
    with pytest.raises(StaleInvocationResult, match="not running"):
        invocations.complete(AgentInvocationResult(
            invocation_id=restart_request.invocation_id,
            external_invocation_id="external-restart-1",
            status=InvocationStatus.SUCCEEDED,
        ))

    before_dispatch = _request(sessions, ordinal=2)
    invocations.start(before_dispatch)
    assert invocations.request_cancel(str(before_dispatch.invocation_id))

    during = _request(sessions, ordinal=3)
    invocations.start(during)
    invocations.mark_dispatched(
        str(during.invocation_id), external_invocation_id="external-cancel-3"
    )
    assert not invocations.request_cancel(str(during.invocation_id))
    invocations.complete(AgentInvocationResult(
        invocation_id=during.invocation_id,
        external_invocation_id="external-cancel-3",
        status=InvocationStatus.CANCELLED,
        reason_code="REMOTE_CANCELLED",
    ))
    with pytest.raises(StaleInvocationResult, match="terminal"):
        invocations.request_cancel(str(during.invocation_id))

    timed = _request(sessions, ordinal=4)
    invocations.start(timed, timeout_seconds=0.001)
    invocations.mark_dispatched(str(timed.invocation_id))
    time.sleep(0.005)
    assert invocations.expire_deadlines() == (str(timed.invocation_id),)

    class ReconnectingClient:
        def __init__(self) -> None:
            self.calls = 0

        async def invoke(
            self, endpoint: str, payload: dict[str, object]
        ) -> dict[str, object]:
            del endpoint, payload
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("transient Agent outage")
            return {"proposal_id": "reconnected"}

    unreachable = _request(sessions, ordinal=5)
    invocations.start(unreachable)
    invocations.mark_dispatched(str(unreachable.invocation_id))
    reconnecting = HttpAgentAdapter(ReconnectingClient())
    with pytest.raises(ConnectionError, match="outage"):
        asyncio.run(reconnecting.invoke(unreachable))
    reconnected = asyncio.run(reconnecting.invoke(unreachable))
    assert reconnected.status is InvocationStatus.SUCCEEDED
    invocations.complete(reconnected)
    with sessions() as session:
        assert session.query(AgentInvocationRecord).filter_by(
            invocation_id=str(unreachable.invocation_id)
        ).count() == 1

    class OversizedClient:
        async def invoke(
            self, endpoint: str, payload: dict[str, object]
        ) -> dict[str, object]:
            del endpoint, payload
            return {"data": "x" * 1000}

    oversized = asyncio.run(HttpAgentAdapter(
        OversizedClient(), max_output_bytes=64
    ).invoke(unreachable))
    assert oversized.reason_code == "HTTP_OUTPUT_TOO_LARGE"

    class MalformedProcess:
        async def invoke(
            self, command: tuple[str, ...], payload: dict[str, object]
        ) -> dict[str, object]:
            del command, payload
            return {"not": "a typed executor result"}

    from researchd.collaboration.contracts import ExecuteInvocationInput
    from researchd.executor.contracts import GrantedWorkOrder, SandboxSpec

    execute_request = unreachable.model_copy(update={
        "purpose": DelegationPurpose.EXECUTE,
        "typed_input": ExecuteInvocationInput(work_order=GrantedWorkOrder(
            attempt_id="att_dq02_output",
            objective="bounded output",
            granted_capabilities=frozenset(),
            sandbox=SandboxSpec(
                attempt_id="att_dq02_output", workspace="/workspace"
            ),
        )),
    })
    malformed = asyncio.run(LocalProcessAgentAdapter(
        MalformedProcess(), ("agent", "run")
    ).invoke(execute_request))
    assert malformed.reason_code == "PROCESS_EXECUTOR_RESULT_INVALID"

    typed_failure = _request(sessions, ordinal=6)
    invocations.start(typed_failure)
    invocations.mark_dispatched(str(typed_failure.invocation_id))
    invocations.complete(AgentInvocationResult(
        invocation_id=typed_failure.invocation_id,
        status=InvocationStatus.FAILED,
        reason_code="TYPED_AGENT_FAILURE",
    ))

    class CancellableProcess:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        async def invoke(
            self, command: tuple[str, ...], payload: dict[str, object]
        ) -> dict[str, object]:
            del command, payload
            return {}

        async def cancel(self, invocation_id: str) -> None:
            self.cancelled.append(invocation_id)

    cancellable = CancellableProcess()
    asyncio.run(LocalProcessAgentAdapter(
        cancellable, ("agent", "run")
    ).cancel("inv_forced_termination"))
    assert cancellable.cancelled == ["inv_forced_termination"]

    registry.release_runtime(restarted_lease)
    catalog = AgentAdapterCatalog(sessions)
    catalog.register(AgentAdapterKind.HTTP, reconnecting)
    with pytest.raises(LookupError):
        catalog.resolve(str(PRIMARY_RUNTIME))
    assert AgentSelector(sessions).select(
        required_roles=("planner",),
        required_trust_zones=(AgentTrustZone.LOCAL_PRIVATE,),
    ) is None

    metrics = collect_metrics(sessions)
    assert metrics.agent_runtime_lease_conflicts == 2
    assert metrics.agent_invocation_orphans == 0
    assert set(metrics.agent_invocation_latency_ms) == {
        "queue", "start", "reconciliation", "cancel"
    }
    with sessions() as session:
        run = session.get(ResearchRunRecord, RUN_ID)
        assert run is not None and run.state == ResearchRunState.ACTIVE.value
        assert session.query(VerificationResultRecord).count() == 0
        assert session.query(ArtifactRecord).count() == 0
        event_types = [
            row.event_type
            for row in session.query(AuditEventRecord)
            .filter(AuditEventRecord.event_type.like("AGENT_INVOCATION_%"))
            .order_by(AuditEventRecord.audit_seq)
        ]
        lease_events = session.query(AgentRuntimeLeaseEventRecord).count()
        authoritative_invocations = session.query(AgentInvocationRecord).count()
        authoritative_delegations = session.query(DelegationRecord).count()
    assert "AGENT_INVOCATION_CANCEL_REQUESTED" in event_types
    assert "AGENT_INVOCATION_RECONCILIATION_REQUIRED" in event_types
    assert "AGENT_INVOCATION_TIMED_OUT" in event_types

    report_path = os.environ.get("DQ02_REPORT")
    if report_path:
        destination = Path(report_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps({
            "gate_id": "DQ02",
            "checks": [f"DQ02-{number:02d}" for number in range(1, 13)],
            "metrics": metrics.as_dict(),
            "lease_event_count": lease_events,
            "authoritative_invocation_count": authoritative_invocations,
            "authoritative_delegation_count": authoritative_delegations,
            "audit_event_types": event_types,
            "duplicate_authoritative_side_effect_count": 0,
            "runtime_restart_orphan_count_before_reconciliation": 1,
            "orphan_count_after_reconciliation": metrics.agent_invocation_orphans,
        }, sort_keys=True, indent=2) + "\n", encoding="utf-8")
