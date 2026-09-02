"""Regression contract for the PH02 HUMAN approval boundary."""

import asyncio
import inspect
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.api.control import LocalControlAPI
from researchd.api.web import ControlCommandRouter
from researchd.collaboration.gateway import CollaborationGateway
from researchd.daemon.contracts import ApprovalApproveCommand, DaemonCommandResult
from researchd.daemon.dispatcher import ControlMutationAuthority, DaemonCommandDispatcher
from researchd.daemon.runtime import ResearchDaemon
from researchd.daemon.startup import StartupBarrier, StartupPhase
from researchd.domain.base import DomainModel
from researchd.domain.enums import ResearchRunState, WorkOrderState
from researchd.orchestrator.driver import OrchestrationDriver, OrchestrationTarget
from researchd.orchestrator.engine import OrchestrationError, ResearchOrchestrator
from researchd.policy.approval import ApprovalService
from researchd.policy.engine import RecordingPolicyEngine
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    ApprovalGrantRecord,
    AuditEventRecord,
    Base,
    ResearchRunRecord,
    WorkOrderRecord,
    WorkspaceRecord,
)
from researchd.supervisor.runtime import RuntimeSupervisor


def _barrier() -> StartupBarrier:
    return StartupBarrier({phase: lambda: None for phase in StartupPhase})


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.commands: list[DomainModel] = []

    def __call__(self, command: DomainModel) -> DaemonCommandResult:
        self.commands.append(command)
        return DaemonCommandResult(
            command_id=str(getattr(command, "command_id")),
            command_type=type(command).__name__.removesuffix("Command"),
            status="ACCEPTED",
            resource={"approval_id": "approval_product"},
        )


def test_human_approval_route_uses_pending_approval_id_not_grant_id(tmp_path: Path) -> None:
    """The public HUMAN intent must never require a client-minted grant ID."""
    sessions = session_factory(create_sqlite_engine(tmp_path / "approval.db"))
    dispatcher = _RecordingDispatcher()
    daemon = ResearchDaemon(_barrier(), dispatcher)
    assert daemon.start().ready
    router = ControlCommandRouter(LocalControlAPI(sessions), daemon)

    status, response = asyncio.run(router.post("/api/approvals/approval_product/approve", {
        "command_id": "cmd_product_approval",
    }))

    assert status == 202
    assert response["status"] == "ACCEPTED"
    assert len(dispatcher.commands) == 1
    command = dispatcher.commands[0]
    assert type(command).__name__ == "ApprovalApproveCommand"
    assert getattr(command, "approval_id") == "approval_product"
    assert getattr(command, "actor_type") == "HUMAN"
    assert getattr(command, "actor_id") == "local-control-client"
    assert not hasattr(command, "grant_id")


def test_legacy_work_order_approve_route_is_absent(tmp_path: Path) -> None:
    """The grant-based work-orders approve route is gone; only the approval route remains."""
    sessions = session_factory(create_sqlite_engine(tmp_path / "approval_route.db"))
    dispatcher = _RecordingDispatcher()
    daemon = ResearchDaemon(_barrier(), dispatcher)
    assert daemon.start().ready
    router = ControlCommandRouter(LocalControlAPI(sessions), daemon)

    status, response = asyncio.run(router.post(
        "/api/work-orders/wo_legacy/approve",
        {"command_id": "cmd_legacy_approve"},
    ))

    assert status == 404
    assert response == {"error": "unknown command"}
    assert dispatcher.commands == []


class _WakeRecordingOrchestrator:
    def __init__(self) -> None:
        self.advanced = threading.Event()
        self.calls: list[str] = []

    async def advance(self, run_id: str) -> bool:
        self.calls.append(run_id)
        self.advanced.set()
        return False


class _ApprovalControl:
    """Answers the dispatcher's approve_request with the resumed run reference."""

    def __init__(self) -> None:
        self.granted_by: list[str] = []

    async def approve_request(self, approval_id: str, *, granted_by: str) -> dict[str, object]:
        assert approval_id == "approval_product"
        self.granted_by.append(granted_by)
        return {"work_order_id": "wo_product", "run_id": "run_product"}


def test_approval_success_wakes_the_orchestration_driver(tmp_path: Path) -> None:
    """An accepted ApprovalApprove must wake the PH01 driver for the resumed run."""
    engine = create_sqlite_engine(tmp_path / "approval_wake.db")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(WorkspaceRecord(
            workspace_id="ws_product", name="Approval wake", version=1,
            created_at=now, updated_at=now,
        ))
    with sessions.begin() as session:
        session.add(ResearchRunRecord(
            run_id="run_product", workspace_id="ws_product",
            objective="resume after approval", state=ResearchRunState.ACTIVE.value,
            version=1, created_at=now, updated_at=now,
        ))
    orchestrator = _WakeRecordingOrchestrator()
    driver = OrchestrationDriver(cast(OrchestrationTarget, orchestrator), sessions)
    control = _ApprovalControl()
    dispatcher = DaemonCommandDispatcher(
        cast(RuntimeSupervisor, object()),
        cast(ControlMutationAuthority, control),
        orchestration_driver=driver,
    )
    daemon = ResearchDaemon(_barrier(), dispatcher, orchestration_driver=driver)
    assert daemon.start().ready
    try:
        outcome = daemon.execute(ApprovalApproveCommand(
            command_id="cmd_product_approval_wake",
            actor_type="HUMAN",
            actor_id="local-control-client",
            approval_id="approval_product",
        ))
        # The approval handler is async; the dispatcher hands back its coroutine.
        assert inspect.iscoroutine(outcome)
        result = asyncio.run(outcome)
        assert isinstance(result, DaemonCommandResult)
        assert result.status == "ACCEPTED"
        assert orchestrator.advanced.wait(timeout=2)
        assert orchestrator.calls == ["run_product"]
        assert control.granted_by == ["local-control-client"]
    finally:
        daemon.stop()


def _seed_pending_approval(
    tmp_path: Path,
) -> tuple[sessionmaker[Session], ApprovalService, str]:
    engine = create_sqlite_engine(tmp_path / "approval_atomic.db")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    approvals = ApprovalService(sessions)
    now = datetime.now(UTC)
    # Each level commits in its own transaction: without ORM relationships
    # the flush order is table-name ordered and the FK-enabled SQLite engine
    # rejects child-first inserts.
    with sessions.begin() as session:
        session.add(WorkspaceRecord(
            workspace_id="ws_approval", name="Approval", version=1,
            created_at=now, updated_at=now,
        ))
    with sessions.begin() as session:
        session.add(ResearchRunRecord(
            run_id="run_approval", workspace_id="ws_approval",
            objective="resume after approval", state=ResearchRunState.WAITING_HUMAN.value,
            version=1, created_at=now, updated_at=now,
        ))
    with sessions.begin() as session:
        session.add(WorkOrderRecord(
            work_order_id="wo_approval", run_id="run_approval",
            objective="elevated capability", state=WorkOrderState.WAITING_APPROVAL.value,
            idempotency_key="idem_approval",
            contract={"requested_capabilities": ["cloud.call"]},
            version=1, created_at=now, updated_at=now,
        ))
    request = approvals.request(
        operation_type="work_order.capabilities",
        parameters={"work_order_id": "wo_approval", "capabilities": ["cloud.call"]},
        requested_by="agent_lead", reason="elevated capability", risk_level="elevated",
        resource_scope={"run_id": "run_approval"}, budget_delta={},
        expires_at=now + timedelta(hours=1),
        run_id="run_approval", work_order_id="wo_approval",
    )
    with sessions.begin() as session:
        order = session.get(WorkOrderRecord, "wo_approval")
        assert order is not None
        order.approval_id = request.approval_id
        order.version += 1
    return sessions, approvals, request.approval_id


def test_engine_approve_request_recovers_a_crashed_grant_atomically(tmp_path: Path) -> None:
    """A crash between grant creation and WorkOrder resumption recovers, never doubles.

    The fault injection commits a grant through ``approve_or_reuse`` while the
    WorkOrder is still WAITING_APPROVAL; the engine's single-transaction
    ``approve_request`` must reuse that grant, resume the Run, and leave
    exactly one consumed grant plus both audit events.
    """
    sessions, approvals, approval_id = _seed_pending_approval(tmp_path)

    # Fault injection: the daemon died after the grant was durably created but
    # before the WorkOrder/Run resumption transaction ran.
    crashed_grant = approvals.approve_or_reuse(approval_id, granted_by="local-control-client")
    with sessions() as session:
        order = session.get(WorkOrderRecord, "wo_approval")
        run = session.get(ResearchRunRecord, "run_approval")
        assert order is not None and run is not None
        assert order.state == WorkOrderState.WAITING_APPROVAL.value
        assert run.state == ResearchRunState.WAITING_HUMAN.value

    orchestrator = ResearchOrchestrator(
        sessions,
        collaboration=cast(CollaborationGateway, object()),
        policy=cast(RecordingPolicyEngine, object()),
        approvals=approvals,
    )
    work_order_id, run_id = orchestrator.approve_request(
        approval_id, granted_by="local-control-client",
    )
    assert (work_order_id, run_id) == ("wo_approval", "run_approval")

    with sessions() as session:
        order = session.get(WorkOrderRecord, "wo_approval")
        run = session.get(ResearchRunRecord, "run_approval")
        assert order is not None and run is not None
        assert order.state == WorkOrderState.POLICY_CHECK.value
        assert order.approval_grant_id == crashed_grant.grant_id
        assert run.state == ResearchRunState.ACTIVE.value
        grants = session.scalars(select(ApprovalGrantRecord)).all()
        assert [grant.grant_id for grant in grants] == [crashed_grant.grant_id]
        assert grants[0].used_at is not None
        events = {
            event.event_type
            for event in session.scalars(
                select(AuditEventRecord).where(AuditEventRecord.run_id == "run_approval"),
            ).all()
        }
        assert {"APPROVAL_GRANTED", "APPROVAL_RESUMED"} <= events

    # A later, different command on the completed approval cannot mint a second grant.
    with pytest.raises(OrchestrationError):
        orchestrator.approve_request(approval_id, granted_by="someone_else")
    with sessions() as session:
        assert len(session.scalars(select(ApprovalGrantRecord)).all()) == 1
