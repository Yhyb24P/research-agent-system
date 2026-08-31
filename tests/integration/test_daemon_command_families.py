"""PX00-05: product typed command families cross the daemon gate."""

import asyncio
import inspect
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, Awaitable, cast

import pytest
from sqlalchemy import select

from researchd.collaboration.contracts import CollaborationMessage
from researchd.daemon.contracts import (
    BackupCreateCommand,
    BackupVerifyCommand,
    CollaborationMessageSendCommand,
    DaemonCommandResult,
    ResearchTaskCreateCommand,
    RestorePlanCommand,
    WorkOrderRejectCommand,
    WorkspaceCreateCommand,
)
from researchd.daemon.dispatcher import DaemonCommandDispatcher
from researchd.domain.base import DomainModel
from researchd.domain.enums import DataClassification
from researchd.domain.ids import AgentId, MessageId
from researchd.storage.models import (
    ApprovalRequestRecord,
    AuditEventRecord,
    CollaborationMessageRecord,
    ResearchRunRecord,
    WorkspaceRecord,
)
from tests.integration.test_daemon import (
    _composed_application,
    _driver_orchestrator,
    _supervisor,
)
from tests.integration.test_orchestrator import _proposal_with_capability


class _FamilyControlStub:
    """Records every control-authority call; mirrors LocalControlAPI shapes."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.messages: list[CollaborationMessage] = []

    async def cancel_run(self, run_id: str) -> dict[str, object]:
        self.calls.append(("cancel_run", run_id))
        return {"run_id": run_id}

    async def approve(self, work_order_id: str, grant_id: str) -> dict[str, object]:
        self.calls.append(("approve", work_order_id, grant_id))
        return {"work_order_id": work_order_id}

    async def approve_request(self, approval_id: str, *, granted_by: str) -> dict[str, object]:
        self.calls.append(("approve_request", approval_id, granted_by))
        return {"approval_id": approval_id}

    def resolve_human(
        self,
        work_order_id: str,
        *,
        action: str,
        objective: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(("resolve_human", work_order_id, action))
        return {"work_order_id": work_order_id}

    def create_workspace(self, workspace_id: str, name: str) -> dict[str, object]:
        self.calls.append(("create_workspace", workspace_id, name))
        return {"workspace_id": workspace_id, "name": name, "version": 1}

    def create_research_task(
        self,
        workspace_id: str,
        objective: str,
        *,
        run_id: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(("create_research_task", workspace_id, objective, run_id))
        return {"run_id": run_id or "run_stub", "workspace_id": workspace_id, "state": "NEW"}

    def reject(
        self,
        work_order_id: str,
        approval_id: str,
        *,
        actor_type: str,
        actor_id: str,
    ) -> dict[str, object]:
        self.calls.append(("reject", work_order_id, approval_id, actor_type, actor_id))
        return {"work_order_id": work_order_id, "state": "FAILED"}

    def send_collaboration_message(self, message: CollaborationMessage) -> dict[str, object]:
        self.messages.append(message)
        self.calls.append(("send_message", str(message.message_id)))
        return {"message_id": str(message.message_id), "run_id": message.run_id, "purpose": message.purpose}


class _BackupStub:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def create_backup(
        self,
        destination: str,
        candidate_commit: str,
        candidate_tag: str,
    ) -> dict[str, object]:
        self.calls.append(("create_backup", destination, candidate_commit, candidate_tag))
        return {"destination": destination, "candidate_commit": candidate_commit}

    def verify_backup(self, snapshot: str) -> dict[str, object]:
        self.calls.append(("verify_backup", snapshot))
        return {"snapshot": snapshot, "healthy": True}

    def plan_restore(
        self,
        snapshot: str,
        database_destination: str,
        artifact_destination: str,
        expected_candidate_commit: str,
        expected_candidate_tag: str,
    ) -> dict[str, object]:
        self.calls.append(("plan_restore", snapshot, database_destination, artifact_destination))
        return {"snapshot": snapshot, "size_bytes": 1}


def _result(value: DomainModel | Awaitable[DomainModel]) -> DomainModel:
    if inspect.isawaitable(value):
        return asyncio.run(cast("Coroutine[Any, Any, DomainModel]", value))
    return value


def test_dispatcher_routes_new_families_in_versioned_envelopes(tmp_path: Path) -> None:
    control = _FamilyControlStub()
    backups = _BackupStub()
    dispatcher = DaemonCommandDispatcher(_supervisor(tmp_path), control, backups=backups)

    workspace = dispatcher(WorkspaceCreateCommand(
        command_id="cmd_family_ws",
        actor_type="HUMAN",
        actor_id="operator",
        workspace_id="ws_family",
        name="family",
    ))
    assert isinstance(workspace, DaemonCommandResult)
    assert workspace.command_type == "WorkspaceCreate"
    assert workspace.status == "ACCEPTED"
    assert workspace.resource == {"workspace_id": "ws_family", "name": "family", "version": 1}

    task = _result(dispatcher(ResearchTaskCreateCommand(
        command_id="cmd_family_task",
        actor_type="HUMAN",
        actor_id="operator",
        workspace_id="ws_family",
        objective="family task",
        run_id="run_family",
    )))
    assert isinstance(task, DaemonCommandResult)
    assert task.command_type == "ResearchTaskCreate"
    assert task.resource == {"run_id": "run_family", "workspace_id": "ws_family", "state": "NEW"}

    rejected = dispatcher(WorkOrderRejectCommand(
        command_id="cmd_family_reject",
        actor_type="HUMAN",
        actor_id="operator",
        work_order_id="wo_family",
        approval_id="apr_family",
    ))
    assert isinstance(rejected, DaemonCommandResult)
    assert rejected.command_type == "WorkOrderReject"
    assert rejected.resource == {"work_order_id": "wo_family", "state": "FAILED"}

    message = dispatcher(CollaborationMessageSendCommand(
        command_id="cmd_family_msg",
        actor_type="HUMAN",
        actor_id="operator",
        message_id="msg_family_1",
        run_id="run_family",
        recipient_agent_id="agent_family",
        purpose="DIRECTIVE",
        body="stay in scope",
    ))
    assert isinstance(message, DaemonCommandResult)
    assert message.command_type == "CollaborationMessageSend"
    sent = control.messages[0]
    assert sent.message_id == MessageId("msg_family_1")
    assert sent.recipient_agent_id == AgentId("agent_family")
    assert sent.sender_actor_type == "HUMAN"
    assert sent.sender_actor_id == "operator"
    assert sent.classification is DataClassification.PROJECT_PRIVATE

    commit = "e" * 40
    tag = "v1.0.0-rc.80"
    created = dispatcher(BackupCreateCommand(
        command_id="cmd_family_backup",
        actor_type="HUMAN",
        actor_id="operator",
        destination="/tmp/snap",
        candidate_commit=commit,
        candidate_tag=tag,
    ))
    assert isinstance(created, DaemonCommandResult)
    assert created.command_type == "BackupCreate"

    verified = dispatcher(BackupVerifyCommand(
        command_id="cmd_family_verify",
        actor_type="HUMAN",
        actor_id="operator",
        snapshot="/tmp/snap",
    ))
    assert isinstance(verified, DaemonCommandResult)
    assert verified.command_type == "BackupVerify"

    planned = dispatcher(RestorePlanCommand(
        command_id="cmd_family_plan",
        actor_type="HUMAN",
        actor_id="operator",
        snapshot="/tmp/snap",
        database_destination="/tmp/restore.db",
        artifact_destination="/tmp/restore-artifacts",
        expected_candidate_commit=commit,
        expected_candidate_tag=tag,
    ))
    assert isinstance(planned, DaemonCommandResult)
    assert planned.command_type == "RestorePlan"

    assert control.calls == [
        ("create_workspace", "ws_family", "family"),
        ("create_research_task", "ws_family", "family task", "run_family"),
        ("reject", "wo_family", "apr_family", "HUMAN", "operator"),
        ("send_message", "msg_family_1"),
    ]
    assert backups.calls[0][:1] == ("create_backup",)


def test_dispatcher_fails_closed_without_backups_authority(tmp_path: Path) -> None:
    dispatcher = DaemonCommandDispatcher(_supervisor(tmp_path), _FamilyControlStub())
    command = BackupCreateCommand(
        command_id="cmd_family_no_backup",
        actor_type="HUMAN",
        actor_id="operator",
        destination="/tmp/snap",
        candidate_commit="e" * 40,
        candidate_tag="v1.0.0-rc.80",
    )

    with pytest.raises(RuntimeError, match="backup mutation authority is not configured"):
        dispatcher(command)


def test_dispatcher_fails_closed_new_families_without_control(tmp_path: Path) -> None:
    dispatcher = DaemonCommandDispatcher(_supervisor(tmp_path))
    command = WorkspaceCreateCommand(
        command_id="cmd_family_no_control",
        actor_type="HUMAN",
        actor_id="operator",
        workspace_id="ws_family",
        name="family",
    )

    with pytest.raises(RuntimeError, match="orchestrator mutation authority is not configured"):
        dispatcher(command)


def test_composed_daemon_creates_workspace_and_research_task(tmp_path: Path) -> None:
    application = _composed_application(tmp_path)

    created = _result(cast(DomainModel | Awaitable[DomainModel], application.daemon.execute(
        WorkspaceCreateCommand(
            command_id="cmd_px05_ws",
            actor_type="HUMAN",
            actor_id="operator",
            workspace_id="ws_px05",
            name="px05",
        ),
    )))
    assert isinstance(created, DaemonCommandResult)
    assert created.command_type == "WorkspaceCreate"
    assert created.status == "ACCEPTED"
    assert created.resource is not None and created.resource["workspace_id"] == "ws_px05"

    task = _result(cast(DomainModel | Awaitable[DomainModel], application.daemon.execute(
        ResearchTaskCreateCommand(
            command_id="cmd_px05_task",
            actor_type="HUMAN",
            actor_id="operator",
            workspace_id="ws_px05",
            objective="px05 task",
        ),
    )))
    assert isinstance(task, DaemonCommandResult)
    assert task.command_type == "ResearchTaskCreate"
    assert task.status == "ACCEPTED"
    assert task.resource is not None
    run_id = str(task.resource["run_id"])
    assert task.resource["state"] == "NEW"

    replay = _result(cast(DomainModel | Awaitable[DomainModel], application.daemon.execute(
        ResearchTaskCreateCommand(
            command_id="cmd_px05_task",
            actor_type="HUMAN",
            actor_id="operator",
            workspace_id="ws_px05",
            objective="px05 task",
        ),
    )))
    assert replay == task

    with application.api.sessions.begin() as session:
        run = session.get(ResearchRunRecord, run_id)
        assert run is not None and run.state == "NEW"
        workspace = session.get(WorkspaceRecord, "ws_px05")
        assert workspace is not None and workspace.name == "px05"


def test_composed_daemon_rejects_pending_approval(tmp_path: Path) -> None:
    application = _composed_application(tmp_path)
    _, driver, _ = _driver_orchestrator(
        application, tmp_path, [_proposal_with_capability("network.external")],
    )
    run_id = driver.create_run(workspace_id="ws_e2e", objective="rejected external step")
    snapshot = asyncio.run(driver.run(run_id, max_steps=10))
    assert snapshot.state.value == "WAITING_HUMAN" and snapshot.pending_approval_ids
    work_order_id = snapshot.work_orders[0][0]
    approval_id = snapshot.pending_approval_ids[0]

    result = _result(cast(DomainModel | Awaitable[DomainModel], application.daemon.execute(
        WorkOrderRejectCommand(
            command_id="cmd_px05_reject",
            actor_type="HUMAN",
            actor_id="operator",
            work_order_id=work_order_id,
            approval_id=approval_id,
        ),
    )))
    assert isinstance(result, DaemonCommandResult)
    assert result.command_type == "WorkOrderReject"
    assert result.status == "ACCEPTED"
    assert application.api.work_order_status(work_order_id)["state"] == "FAILED"
    assert application.api.run_status(run_id)["state"] == "FAILED"
    with application.api.sessions() as session:
        request = session.get(ApprovalRequestRecord, approval_id)
        assert request is not None and request.status == "REJECTED"
        events = session.scalars(select(AuditEventRecord).where(
            AuditEventRecord.event_type == "APPROVAL_REJECTED",
        ).order_by(AuditEventRecord.audit_seq)).all()
    # The work-order transition records the operator; the run transition
    # records the orchestrator.
    assert [(event.entity_type, event.actor_id) for event in events] == [
        ("work_order", "operator"),
        ("research_run", "orchestrator"),
    ]

    # A second rejection through a fresh command identity fails closed: the
    # order is no longer awaiting approval.
    again = _result(cast(DomainModel | Awaitable[DomainModel], application.daemon.execute(
        WorkOrderRejectCommand(
            command_id="cmd_px05_reject_again",
            actor_type="HUMAN",
            actor_id="operator",
            work_order_id=work_order_id,
            approval_id=approval_id,
        ),
    )))
    assert isinstance(again, DaemonCommandResult)
    assert again.status == "REJECTED"
    assert again.reason_code == "OrchestrationError"


def test_composed_daemon_sends_collaboration_message(tmp_path: Path) -> None:
    application = _composed_application(tmp_path)
    orchestrator = application.api.orchestrator
    assert orchestrator is not None
    _result(cast(DomainModel | Awaitable[DomainModel], application.daemon.execute(
        WorkspaceCreateCommand(
            command_id="cmd_px05_msg_ws",
            actor_type="HUMAN",
            actor_id="operator",
            workspace_id="ws_px05_msg",
            name="px05 messages",
        ),
    )))
    _result(cast(DomainModel | Awaitable[DomainModel], application.daemon.execute(
        ResearchTaskCreateCommand(
            command_id="cmd_px05_msg_task",
            actor_type="HUMAN",
            actor_id="operator",
            workspace_id="ws_px05_msg",
            objective="px05 messaging",
            run_id="run_px05_msg",
        ),
    )))

    result = _result(cast(DomainModel | Awaitable[DomainModel], application.daemon.execute(
        CollaborationMessageSendCommand(
            command_id="cmd_px05_msg",
            actor_type="HUMAN",
            actor_id="operator",
            message_id="msg_px05_1",
            run_id="run_px05_msg",
            purpose="DIRECTIVE",
            body="keep the experiment bounded",
        ),
    )))
    assert isinstance(result, DaemonCommandResult)
    assert result.command_type == "CollaborationMessageSend"
    assert result.status == "ACCEPTED"
    with application.api.sessions() as session:
        record = session.get(CollaborationMessageRecord, "msg_px05_1")
        assert record is not None
        assert record.sender_actor_type == "HUMAN"
        assert record.sender_actor_id == "operator"
        assert record.body == "keep the experiment bounded"
        events = session.scalars(select(AuditEventRecord).where(
            AuditEventRecord.event_type == "HUMAN_DIRECTIVE_RECORDED",
        )).all()
    assert [event.entity_id for event in events] == ["msg_px05_1"]

    # The same message identity through a different command identity collides
    # on the append-only store and is durably rejected.
    collision = _result(cast(DomainModel | Awaitable[DomainModel], application.daemon.execute(
        CollaborationMessageSendCommand(
            command_id="cmd_px05_msg_collision",
            actor_type="HUMAN",
            actor_id="operator",
            message_id="msg_px05_1",
            run_id="run_px05_msg",
            purpose="DIRECTIVE",
            body="keep the experiment bounded",
        ),
    )))
    assert isinstance(collision, DaemonCommandResult)
    assert collision.status == "REJECTED"


def test_composed_daemon_backup_lifecycle_is_dry_and_durable(tmp_path: Path) -> None:
    application = _composed_application(tmp_path)
    commit = "e" * 40
    tag = "v1.0.0-rc.80"
    destination = tmp_path / "snapshots" / "px05"

    created = _result(cast(DomainModel | Awaitable[DomainModel], application.daemon.execute(
        BackupCreateCommand(
            command_id="cmd_px05_backup",
            actor_type="HUMAN",
            actor_id="operator",
            destination=str(destination),
            candidate_commit=commit,
            candidate_tag=tag,
        ),
    )))
    assert isinstance(created, DaemonCommandResult)
    assert created.command_type == "BackupCreate"
    assert created.status == "ACCEPTED"
    assert (destination / "manifest.json").is_file()
    assert (destination / "research.db").is_file()

    verified = _result(cast(DomainModel | Awaitable[DomainModel], application.daemon.execute(
        BackupVerifyCommand(
            command_id="cmd_px05_verify",
            actor_type="HUMAN",
            actor_id="operator",
            snapshot=str(destination),
        ),
    )))
    assert isinstance(verified, DaemonCommandResult)
    assert verified.command_type == "BackupVerify"
    assert verified.status == "ACCEPTED"
    assert verified.resource is not None and verified.resource["healthy"] is True

    planned = _result(cast(DomainModel | Awaitable[DomainModel], application.daemon.execute(
        RestorePlanCommand(
            command_id="cmd_px05_plan",
            actor_type="HUMAN",
            actor_id="operator",
            snapshot=str(destination),
            database_destination=str(tmp_path / "restore.db"),
            artifact_destination=str(tmp_path / "restore-artifacts"),
            expected_candidate_commit=commit,
            expected_candidate_tag=tag,
        ),
    )))
    assert isinstance(planned, DaemonCommandResult)
    assert planned.command_type == "RestorePlan"
    assert planned.status == "ACCEPTED"
    plan = cast(dict[str, object], planned.resource)
    assert plan["candidate_commit"] == commit
    assert cast(int, plan["size_bytes"]) > 0
    # Planning is a dry run: nothing is copied.
    assert not (tmp_path / "restore.db").exists()
    assert not (tmp_path / "restore-artifacts").exists()

    mismatched = _result(cast(DomainModel | Awaitable[DomainModel], application.daemon.execute(
        RestorePlanCommand(
            command_id="cmd_px05_plan_mismatch",
            actor_type="HUMAN",
            actor_id="operator",
            snapshot=str(destination),
            database_destination=str(tmp_path / "restore2.db"),
            artifact_destination=str(tmp_path / "restore2-artifacts"),
            expected_candidate_commit="f" * 40,
            expected_candidate_tag=tag,
        ),
    )))
    assert isinstance(mismatched, DaemonCommandResult)
    assert mismatched.status == "REJECTED"
    assert mismatched.reason_code == "BackupError"


def test_composed_daemon_rejects_unknown_workspace(tmp_path: Path) -> None:
    application = _composed_application(tmp_path)

    result = _result(cast(DomainModel | Awaitable[DomainModel], application.daemon.execute(
        ResearchTaskCreateCommand(
            command_id="cmd_px05_task_missing_ws",
            actor_type="HUMAN",
            actor_id="operator",
            workspace_id="ws_does_not_exist",
            objective="orphan task",
        ),
    )))
    assert isinstance(result, DaemonCommandResult)
    assert result.status == "REJECTED"
