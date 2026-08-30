"""PX05-04/06: external handoff decision actor binding, durable receipt +
audit of HUMAN decisions, and read-only run/agent filtering of the console."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.api.control import LocalControlAPI
from researchd.collaboration.contracts import AgentProfile, AgentRuntime
from researchd.collaboration.handoff import HandoffResolutionService
from researchd.collaboration.registry import AgentRegistryService
from researchd.daemon.command_service import DurableDaemonCommandService
from researchd.daemon.contracts import ExternalHandoffDecisionRequest, HandoffDecisionCommand
from researchd.daemon.dispatcher import DaemonCommandDispatcher
from researchd.domain.enums import AgentAdapterKind, AgentTrustZone, DelegationPurpose
from researchd.domain.ids import AgentId, AgentRuntimeId
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    AgentInvocationRecord,
    AgentRecord,
    AgentRuntimeRecord,
    AuditEventRecord,
    AttemptRecord,
    CollaborationMessageRecord,
    DaemonCommandRecord,
    DelegationRecord,
    HandoffProposalRecord,
    ResearchRunRecord,
    WorkspaceRecord,
    WorkOrderRecord,
)
from researchd.supervisor.runtime import RuntimeSupervisor
from researchd.runtime_sessions.service import RuntimeSessionService
from tests.integration.test_handoff_safety import Fixture
from tests.integration.test_storage import migrate


def _supervisor(sessions: sessionmaker[Session]) -> RuntimeSupervisor:
    return RuntimeSupervisor(RuntimeSessionService(sessions, AgentRegistryService(sessions)))


def _seed_two_runs(tmp_path: Path) -> sessionmaker[Session]:
    """Two runs, each with one Agent's execution invocation and handoff."""
    database = tmp_path / "console.db"
    migrate(database)
    sessions = session_factory(create_sqlite_engine(database))
    registry = AgentRegistryService(sessions)
    for agent_id, runtime_id in (("agent_a", "runtime_a"), ("agent_b", "runtime_b")):
        registry.register_profile(AgentProfile(
            agent_id=AgentId(agent_id), display_name=agent_id, roles=("executor",),
            trust_zone=AgentTrustZone.LOCAL_PRIVATE,
        ))
        registry.register_runtime(AgentRuntime(
            runtime_id=AgentRuntimeId(runtime_id), agent_id=AgentId(agent_id),
            adapter_kind=AgentAdapterKind.INTERNAL, runtime_name=agent_id,
        ))
        registry.acquire_runtime(runtime_id, owner_id="console-fixture", lease_seconds=3600)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(WorkspaceRecord(workspace_id="ws_c", name="console", version=1, created_at=now, updated_at=now))
        session.flush()
        for run_id, agent_id, runtime_id in (("run_alpha", "agent_a", "runtime_a"), ("run_beta", "agent_b", "runtime_b")):
            suffix = "a" if run_id == "run_alpha" else "b"
            session.add(ResearchRunRecord(
                run_id=run_id, workspace_id="ws_c", objective=f"{run_id} objective",
                state="ACTIVE", version=1, created_at=now, updated_at=now,
            ))
            session.flush()
            session.add(WorkOrderRecord(
                work_order_id=f"wo_{suffix}", run_id=run_id, objective=f"execute {suffix}",
                state="EXECUTING", idempotency_key=f"console-wo-{suffix}",
                contract={
                    "proposal_id": f"wo_{suffix}", "objective": f"execute {suffix}",
                    "inputs": [], "requested_capabilities": [],
                    "constraints": {"network": "none", "writable_paths": []},
                    "budget": {"max_wall_seconds": 60}, "acceptance": [],
                    "expected_outputs": [],
                    "data_policy": {"default_classification": "LOCAL_ONLY"},
                    "evidence_refs": [],
                },
                version=1, created_at=now, updated_at=now,
            ))
            session.flush()
            session.add(DelegationRecord(
                delegation_id=f"del_{suffix}", run_id=run_id, work_order_id=f"wo_{suffix}",
                purpose=DelegationPurpose.EXECUTE.value, required_roles_json=["executor"],
                assigned_agent_id=agent_id, assigned_runtime_id=runtime_id,
                state="RUNNING", idempotency_key=f"console-del-{suffix}", version=1,
                created_at=now, updated_at=now,
            ))
            session.flush()
            session.add(AttemptRecord(
                attempt_id=f"att_{suffix}", work_order_id=f"wo_{suffix}",
                delegation_id=f"del_{suffix}", state="RUNNING", terminal_at=None,
                version=1, created_at=now, updated_at=now,
            ))
            session.flush()
            runtime = session.get(AgentRuntimeRecord, runtime_id)
            assert runtime is not None
            session.add(AgentInvocationRecord(
                invocation_id=f"inv_{suffix}", delegation_id=f"del_{suffix}",
                run_id=run_id, work_order_id=f"wo_{suffix}", attempt_id=f"att_{suffix}",
                agent_id=agent_id, runtime_id=runtime_id,
                runtime_lease_id=runtime.runtime_lease_id,
                purpose=DelegationPurpose.EXECUTE.value, status="RUNNING",
                input_sha256="0" * 64, created_at=now,
            ))
            session.flush()
            session.add(HandoffProposalRecord(
                proposal_id=f"handoff_{suffix}", action_id=f"act_{suffix}",
                run_id=run_id, work_order_id=f"wo_{suffix}",
                source_delegation_id=f"del_{suffix}", source_invocation_id=f"inv_{suffix}",
                source_agent_id=agent_id, proposed_target_agent_id=None,
                requested_mode="CONTINUE", reason=f"continue {suffix}",
                continuation_objective=None, artifact_ids_json=[], observation_ids_json=[],
                status="PROPOSED", created_at=now,
            ))
            session.add(CollaborationMessageRecord(
                message_id=f"msg_{suffix}", run_id=run_id, work_order_id=f"wo_{suffix}",
                delegation_id=f"del_{suffix}", invocation_id=f"inv_{suffix}",
                sender_actor_type="agent", sender_actor_id=agent_id,
                recipient_agent_id=None, purpose="STATUS", body=f"status {suffix}",
                classification="PROJECT_PRIVATE", metadata_json={}, created_at=now,
            ))
    return sessions


def test_external_handoff_decision_request_rejects_forged_actor_fields() -> None:
    payload = {
        "command_id": "cmd_handoff_forged",
        "decision": "accept",
        "reason": "take over",
        "target_agent_id": "agent_b",
        "actor_type": "SYSTEM",
        "actor_id": "forged-system",
    }
    with pytest.raises(ValidationError):
        ExternalHandoffDecisionRequest.model_validate(payload)
    # The untrusted request model carries no actor identity at all.
    clean = ExternalHandoffDecisionRequest.model_validate({
        "command_id": "cmd_handoff_clean", "decision": "accept", "reason": "take over",
    })
    assert not hasattr(clean, "actor_type") and not hasattr(clean, "actor_id")
    # A rejected handoff may not select a target Agent.
    with pytest.raises(ValidationError):
        ExternalHandoffDecisionRequest.model_validate({
            "command_id": "cmd_handoff_reject", "decision": "reject",
            "reason": "no", "target_agent_id": "agent_b",
        })


def test_handoff_decision_receipt_and_audit_record_human_actor(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    proposal_id = fixture.proposal()
    fixture.terminalize_source()
    sessions = fixture.sessions
    handoffs = HandoffResolutionService(sessions, fixture.controller())
    dispatcher = DaemonCommandDispatcher(_supervisor(sessions), handoffs=handoffs)
    durable = DurableDaemonCommandService(sessions, dispatcher)
    command = HandoffDecisionCommand(
        command_id="cmd_handoff_accept", actor_type="HUMAN", actor_id="local-control-client",
        proposal_id=proposal_id, decision="accept", reason="take over",
        target_agent_id="agent_b",
    )

    result = asyncio.run(durable.execute(command))

    assert result.status == "ACCEPTED"
    with sessions() as session:
        receipt = session.get(DaemonCommandRecord, "cmd_handoff_accept")
        assert receipt is not None
        assert receipt.actor_type == "HUMAN" and receipt.actor_id == "local-control-client"
        assert receipt.status == "COMPLETED"
        accepted = session.scalar(select(AuditEventRecord).where(
            AuditEventRecord.event_type == "DAEMON_COMMAND_ACCEPTED",
            AuditEventRecord.entity_id == "cmd_handoff_accept",
        ))
        assert accepted is not None and accepted.actor_type == "HUMAN"
        assert accepted.actor_id == "local-control-client"
        completed = session.scalar(select(AuditEventRecord).where(
            AuditEventRecord.event_type == "DAEMON_COMMAND_COMPLETED",
            AuditEventRecord.entity_id == "cmd_handoff_accept",
        ))
        assert completed is not None
        handoff_audit = session.scalar(select(AuditEventRecord).where(
            AuditEventRecord.event_type == "HANDOFF_ACCEPTED",
        ))
        assert handoff_audit is not None and handoff_audit.actor_type == "HUMAN"
        row = session.get(HandoffProposalRecord, proposal_id)
        assert row is not None
        assert row.status == "ACCEPTED"
        assert row.decision_actor_type == "HUMAN" and row.decision_actor_id == "local-control-client"


def test_api_handoffs_are_filtered_by_run(tmp_path: Path) -> None:
    sessions = _seed_two_runs(tmp_path)
    api = LocalControlAPI(sessions)

    alpha = api.handoffs("run_alpha")
    beta = api.handoffs("run_beta")
    everything = api.handoffs()

    assert [item["proposal_id"] for item in alpha] == ["handoff_a"]
    assert [item["proposal_id"] for item in beta] == ["handoff_b"]
    assert {item["proposal_id"] for item in everything} == {"handoff_a", "handoff_b"}
    with pytest.raises(LookupError):
        api.handoffs("run_missing")


def test_agent_console_is_filtered_by_agent_and_run_and_read_only(tmp_path: Path) -> None:
    sessions = _seed_two_runs(tmp_path)
    api = LocalControlAPI(sessions)

    def counts() -> dict[str, int]:
        with sessions() as session:
            return {
                "delegations": len(list(session.scalars(select(DelegationRecord)).all())),
                "invocations": len(list(session.scalars(select(AgentInvocationRecord)).all())),
                "messages": len(list(session.scalars(select(CollaborationMessageRecord)).all())),
                "handoffs": len(list(session.scalars(select(HandoffProposalRecord)).all())),
            }

    before = counts()
    console = api.agent_console("agent_a", run_id="run_alpha")
    assert console["agent"]["agent_id"] == "agent_a"
    assert [item["proposal_id"] for item in console["handoffs"]] == ["handoff_a"]
    assert [item["invocation_id"] for item in console["invocations"]] == ["inv_a"]
    assert [item["delegation_id"] for item in console["delegations"]] == ["del_a"]
    assert [item["message_id"] for item in console["messages"]] == ["msg_a"]
    # agent_a has no activity in run_beta, so the run filter empties it.
    empty = api.agent_console("agent_a", run_id="run_beta")
    assert empty["handoffs"] == [] and empty["invocations"] == []
    assert counts() == before

    with pytest.raises(LookupError):
        api.agent_console("agent_missing")
    with pytest.raises(LookupError):
        api.agent_console("agent_a", run_id="run_missing")
