"""PX06: end-to-end multi-role closed loop, coder handoff accept/reject,
three concurrent consoles, client-exit independence, and SSE resume."""

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.api.control import LocalControlAPI
from researchd.api.web import ControlResourceRouter, serve_local_control
from researchd.artifacts.provenance import ArtifactService
from researchd.artifacts.store import ContentAddressedArtifactStore
from researchd.collaboration.action_broker import AgentActionBroker
from researchd.collaboration.contracts import AgentProfile, AgentRuntime
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.gateway import CollaborationGateway
from researchd.collaboration.handoff import HandoffResolutionService
from researchd.collaboration.heterogeneous import ManagedProcessAgentAdapter
from researchd.collaboration.invocation import InvocationService
from researchd.collaboration.registry import AgentRegistryService
from researchd.collaboration.runtime import AgentAdapterCatalog
from researchd.collaboration.selector import AgentSelector
from researchd.domain.enums import (
    AgentAdapterKind,
    AgentTrustZone,
    AttemptState,
    Capability,
    DelegationPurpose,
    HandoffStatus,
    ResearchRunState,
    WorkOrderState,
)
from researchd.domain.ids import AgentId, AgentRuntimeId
from researchd.executor.capability_broker import CapabilityBroker
from researchd.executor.contracts import CommandLimits, CommandResult, CommandSpec, SandboxSpec
from researchd.orchestrator.engine import OrchestrationError, OrchestrationLimits, ResearchOrchestrator
from researchd.policy.engine import BudgetLimits, DeterministicPolicyEngine, RecordingPolicyEngine
from researchd.runtime_sessions.contracts import ProcessLaunchSpec
from researchd.runtime_sessions.launch_profiles import RuntimeLaunchProfileService
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    AgentInvocationRecord,
    AgentRecord,
    AgentRuntimeRecord,
    AuditEventRecord,
    AttemptRecord,
    DelegationRecord,
    HandoffProposalRecord,
    ResearchRunRecord,
    RuntimeSessionRecord,
    WorkspaceGrantRecord,
    WorkspaceRecord,
    WorkspaceTransportRecord,
    WorkOrderRecord,
)
from tests.integration.test_orchestrator import FakeVerifier
from tests.integration.test_storage import migrate

ENDPOINT = "http://127.0.0.1:9100/turn"


def _plan_output() -> dict[str, Any]:
    return {
        "proposal_id": "plan_e2e",
        "hypotheses": [],
        "proposed_work_orders": [{
            "proposal_id": "wo_e2e",
            "objective": "write the evidence file",
            "inputs": [],
            "requested_capabilities": ["workspace.write"],
            "constraints": {"network": "none", "writable_paths": []},
            "budget": {"max_wall_seconds": 60},
            "acceptance": [],
            "expected_outputs": [],
            "data_policy": {"default_classification": "LOCAL_ONLY"},
            "evidence_refs": [],
        }],
        "risks": [],
        "required_evidence": [],
    }


def _handoff_action(action_id: str = "act_handoff") -> dict[str, Any]:
    return {
        "kind": "handoff",
        "action_id": action_id,
        "requested_mode": "CONTINUE",
        "reason": "take over the remaining work",
        "proposed_target_agent_id": "agent_coder_b",
    }


class FakeSandboxBackend:
    def __init__(self) -> None:
        self.commands: list[CommandSpec] = []

    def run(self, sandbox: SandboxSpec, command: CommandSpec) -> CommandResult:
        self.commands.append(command)
        return CommandResult(
            execution_id=command.execution_id, exit_code=0, stdout=b"ok",
            stderr=b"", timed_out=False, cancelled=False,
            output_limit_exceeded=False, duration_seconds=0.1,
        )

    def cancel(self, execution_id: str) -> bool:
        del execution_id
        return False


class RoutingClient:
    """HttpAgentClient double: routes managed turns by purpose/attempt."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.exec_queues: dict[str, list[dict[str, Any]]] = {}

    def queue_executions(self, attempt_id: str, responses: list[dict[str, Any]]) -> None:
        self.exec_queues[attempt_id] = list(responses)

    async def invoke(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        del endpoint
        self.requests.append(payload)
        purpose = payload["purpose"]
        if purpose == "PLAN":
            return {"output": _plan_output()}
        if purpose == "REVIEW":
            context = payload["payload"]
            return {"output": {
                "decision": "ACCEPT",
                "work_order_id": payload["work_order_id"],
                "evidence_refs": [context["verification_id"]],
                "deficiencies": [],
                "rationale": "verification is green",
                "requested_next_objective": None,
                "requested_evidence": [],
            }}
        queue = self.exec_queues.get(payload["attempt_id"])
        assert queue, f"no scripted execution response for attempt {payload['attempt_id']}"
        return queue.pop(0)


class E2EFixture:
    """Managed planner/coder/coder-b/reviewer Agents driving one orchestrator."""

    AGENTS = (
        ("agent_planner", ("planner",)),
        ("agent_coder", ("executor",)),
        ("agent_coder_b", ("executor",)),
        ("agent_reviewer", ("reviewer",)),
    )

    def __init__(self, tmp_path: Path) -> None:
        database = tmp_path / "e2e.db"
        migrate(database)
        self.sessions: sessionmaker[Session] = session_factory(create_sqlite_engine(database))
        self.workspace_dir = tmp_path / "e2e-workspace"
        self.workspace_dir.mkdir()
        registry = AgentRegistryService(self.sessions)
        for agent_id, roles in self.AGENTS:
            runtime_id = f"runtime_{agent_id}"
            registry.register_profile(AgentProfile(
                agent_id=AgentId(agent_id), display_name=agent_id, roles=roles,
                trust_zone=AgentTrustZone.LOCAL_PRIVATE,
            ))
            registry.register_runtime(AgentRuntime(
                runtime_id=AgentRuntimeId(runtime_id), agent_id=AgentId(agent_id),
                adapter_kind=AgentAdapterKind.PROCESS, runtime_name=agent_id,
                endpoint_ref=ENDPOINT,
            ))
            registry.acquire_runtime(runtime_id, owner_id="e2e-fixture", lease_seconds=3600)
        self.launch_profiles = RuntimeLaunchProfileService(self.sessions, registry)
        for agent_id, _ in self.AGENTS:
            runtime_id = f"runtime_{agent_id}"
            self.launch_profiles.register_process(
                runtime_id, ProcessLaunchSpec(argv=("/usr/bin/true",), cwd="/tmp"),
            )
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            session.add(WorkspaceRecord(workspace_id="ws_e2e", name="e2e", version=1, created_at=now, updated_at=now))
            session.flush()
            for agent_id, _ in self.AGENTS:
                runtime_id = f"runtime_{agent_id}"
                session.add(RuntimeSessionRecord(
                    runtime_session_id=f"rs_{agent_id}", runtime_id=runtime_id,
                    launch_mode="PROCESS", supervisor_state="HEALTHY",
                    launch_spec_json={"argv": ["/usr/bin/true"], "cwd": "/tmp"},
                    launch_profile_sha256=self.launch_profiles.get(runtime_id).spec_sha256,
                    reattach_state="NOT_APPLICABLE", started_at=now, last_health_at=now,
                    version=1, created_at=now, updated_at=now,
                ))
        self.backend = FakeSandboxBackend()
        self.client = RoutingClient()
        self.broker = CapabilityBroker(
            self.backend,
            ArtifactService(ContentAddressedArtifactStore(tmp_path / "artifacts"), self.sessions),
            self.sessions,
            command_limits=CommandLimits(
                wall_seconds=10, cpu_seconds=8, memory_mb=768,
                file_size_mb=16, output_bytes=128_000,
            ),
        )
        self.adapter = ManagedProcessAgentAdapter(
            self.sessions, self.launch_profiles, self.client, self.broker,
            AgentActionBroker(self.sessions),
        )
        catalog = AgentAdapterCatalog(self.sessions)
        catalog.register(AgentAdapterKind.PROCESS, self.adapter)
        self.gateway = CollaborationGateway(
            None, None,
            delegations=DelegationService(self.sessions),
            invocations=InvocationService(self.sessions),
            selector=AgentSelector(self.sessions),
            catalog=catalog,
        )
        self.orchestrator = ResearchOrchestrator(
            self.sessions, collaboration=self.gateway,
            policy=RecordingPolicyEngine(DeterministicPolicyEngine(), self.sessions),
            verifier=FakeVerifier(self.sessions),
            workspace_capabilities=frozenset({Capability.WORKSPACE_WRITE}),
            user_capabilities=frozenset({Capability.WORKSPACE_WRITE}),
            maximum_budget=BudgetLimits(100, 100, 0, 100, 100),
            limits=OrchestrationLimits(max_iterations=8, max_cloud_calls=8),
        )

    def create_run(self) -> str:
        return self.orchestrator.create_run(workspace_id="ws_e2e", objective="managed closed loop")

    def latest_order(self, run_id: str) -> WorkOrderRecord:
        with self.sessions() as session:
            row = session.scalar(select(WorkOrderRecord).where(
                WorkOrderRecord.run_id == run_id,
            ).order_by(WorkOrderRecord.created_at.desc()).limit(1))
        assert row is not None
        return row

    def latest_attempt(self, run_id: str) -> AttemptRecord:
        order = self.latest_order(run_id)
        with self.sessions() as session:
            row = session.scalar(select(AttemptRecord).where(
                AttemptRecord.work_order_id == order.work_order_id,
            ).order_by(AttemptRecord.created_at.desc()).limit(1))
        assert row is not None
        return row

    def advance_until_executing(self, run_id: str) -> None:
        for _ in range(16):
            progressed = asyncio.run(self.orchestrator.advance(run_id))
            if self.latest_order(run_id).state == WorkOrderState.EXECUTING.value:
                return
            if not progressed:
                raise OrchestrationError("run stalled before execution")
        raise OrchestrationError("run did not reach execution")

    def seed_grant_for(self, attempt_id: str) -> None:
        with self.sessions() as session:
            attempt = session.get(AttemptRecord, attempt_id)
            assert attempt is not None and attempt.delegation_id is not None
            delegation_id = attempt.delegation_id
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            session.add(WorkspaceGrantRecord(
                workspace_grant_id=f"grant_{delegation_id}", delegation_id=delegation_id,
                source_workspace_id="ws_e2e", source_revision="1",
                source_manifest_sha256="e" * 64,
                access_mode="READ_WRITE", allowed_paths=["."], excluded_paths=[],
                classification_ceiling="LOCAL_ONLY",
                max_total_bytes=10_000_000, max_file_count=1_000, max_single_file_bytes=5_000_000,
                lease_seconds=3600, lease_started_at=now, lease_expires_at=now + timedelta(hours=1),
                renewal_policy="DENY", transport_kind="ARCHIVE",
                reconciliation_mode="ARTIFACT_ONLY", state="ACTIVE",
                cleanup_state="PENDING", version=1, created_at=now, updated_at=now,
            ))
            session.add(WorkspaceTransportRecord(
                workspace_transport_id=f"wst_{delegation_id}",
                workspace_grant_id=f"grant_{delegation_id}",
                transport_kind="ARCHIVE", transport_handle={"root": str(self.workspace_dir)},
                remote_workspace_handle=str(self.workspace_dir), state="ACTIVE", created_at=now,
            ))


@pytest.fixture
def e2e(tmp_path: Path) -> E2EFixture:
    return E2EFixture(tmp_path)


def test_managed_multi_role_closed_loop_completes(e2e: E2EFixture) -> None:
    fixture = e2e
    run_id = fixture.create_run()
    fixture.advance_until_executing(run_id)
    attempt = fixture.latest_attempt(run_id)
    fixture.seed_grant_for(attempt.attempt_id)
    fixture.client.queue_executions(attempt.attempt_id, [
        {"execution": {"actions": [{
            "request_id": "cap_write", "capability": "workspace.write",
            "parameters": {"path": "evidence.txt", "content": "e2e evidence"},
        }]}},
        {"execution": {"final_claim": "e2e execution complete"}},
    ])

    snapshot = asyncio.run(fixture.orchestrator.run(run_id, max_steps=30))

    assert snapshot.state.value == ResearchRunState.COMPLETED.value
    assert (fixture.workspace_dir / "evidence.txt").read_text() == "e2e evidence"
    with fixture.sessions() as session:
        delegations = {
            row.purpose: row for row in session.scalars(
                select(DelegationRecord).where(DelegationRecord.run_id == run_id),
            ).all()
        }
        assert delegations["PLAN"].assigned_agent_id == "agent_planner"
        assert delegations["EXECUTE"].assigned_agent_id == "agent_coder"
        assert delegations["REVIEW"].assigned_agent_id == "agent_reviewer"
        invocations = {
            row.purpose: row for row in session.scalars(
                select(AgentInvocationRecord).where(AgentInvocationRecord.run_id == run_id),
            ).all()
        }
        assert invocations["PLAN"].output_type == "PlanProposal"
        assert invocations["EXECUTE"].output_type == "ExecutorResult"
        assert invocations["REVIEW"].output_type == "ReviewDecision"
        assert all(row.status == "SUCCEEDED" for row in invocations.values())
        names = {
            event.event_type for event in session.scalars(
                select(AuditEventRecord).where(AuditEventRecord.run_id == run_id),
            ).all()
        }
        assert {
            "PLAN_CREATED", "WORK_ORDER_DISPATCHED", "EXECUTION_STARTED",
            "VERIFICATION_COMPLETED", "REVIEW_DECISION_RECORDED",
            "WORK_ORDER_ACCEPTED", "RUN_COMPLETED",
        } <= names


def test_coder_handoff_accept_continues_with_target_agent(e2e: E2EFixture) -> None:
    fixture = e2e
    run_id = fixture.create_run()
    fixture.advance_until_executing(run_id)
    attempt = fixture.latest_attempt(run_id)
    fixture.seed_grant_for(attempt.attempt_id)
    # The coder proposes a CONTINUE handoff to the backup executor through
    # the action broker while its turn is live, then the turn fails closed
    # (the follow-up model call omits the execution result).
    fixture.client.queue_executions(attempt.attempt_id, [
        {"execution": {"actions": [{
            "request_id": "cap_write", "capability": "workspace.write",
            "parameters": {"path": "evidence.txt", "content": "partial evidence"},
        }]}, "agent_actions": [_handoff_action()]},
        {"output": {"note": "stuck; handing off"}},
    ])
    with pytest.raises(ValueError, match="model_unavailable"):
        asyncio.run(fixture.orchestrator.advance(run_id))

    # Reconcile the crashed execution the way the controller would: the
    # invocation/delegation are already terminal via the invocation service.
    now = datetime.now(UTC)
    with fixture.sessions.begin() as session:
        row = session.get(AttemptRecord, attempt.attempt_id)
        assert row is not None
        row.state = AttemptState.FAILED.value
        row.terminal_at = now
        order = session.get(WorkOrderRecord, row.work_order_id)
        assert order is not None
        order.state = WorkOrderState.EXECUTION_FAILED.value
        order.version += 1
        order.updated_at = now
    with fixture.sessions() as session:
        proposal = session.scalar(select(HandoffProposalRecord).where(
            HandoffProposalRecord.run_id == run_id,
        ))
        assert proposal is not None and proposal.status == HandoffStatus.PROPOSED.value
        proposal_id = proposal.proposal_id

    service = HandoffResolutionService(fixture.sessions, fixture.orchestrator)
    resolved = service.accept(
        proposal_id, actor_type="HUMAN", actor_id="operator",
        reason="take over", target_agent_id="agent_coder_b",
    )
    assert resolved.status is HandoffStatus.ACCEPTED
    assert resolved.resolution_entity_type == "attempt"
    new_attempt_id = cast(str, resolved.resolution_entity_id)
    fixture.seed_grant_for(new_attempt_id)
    fixture.client.queue_executions(new_attempt_id, [
        {"execution": {"actions": [{
            "request_id": "cap_write_b", "capability": "workspace.write",
            "parameters": {"path": "evidence_b.txt", "content": "takeover evidence"},
        }]}},
        {"execution": {"final_claim": "e2e takeover complete"}},
    ])

    snapshot = asyncio.run(fixture.orchestrator.run(run_id, max_steps=30))

    assert snapshot.state.value == ResearchRunState.COMPLETED.value
    assert (fixture.workspace_dir / "evidence_b.txt").read_text() == "takeover evidence"
    with fixture.sessions() as session:
        new_attempt = session.get(AttemptRecord, new_attempt_id)
        assert new_attempt is not None
        delegation = session.get(DelegationRecord, new_attempt.delegation_id)
        assert delegation is not None
        assert delegation.assigned_agent_id == "agent_coder_b"
        assert delegation.purpose == DelegationPurpose.EXECUTE.value
        proposal_row = session.get(HandoffProposalRecord, proposal_id)
        assert proposal_row is not None
        assert proposal_row.status == HandoffStatus.ACCEPTED.value
        assert proposal_row.decision_actor_type == "HUMAN" and proposal_row.decision_actor_id == "operator"
        assert session.scalar(select(AuditEventRecord.event_type).where(
            AuditEventRecord.event_type == "HANDOFF_ACCEPTED",
        )) == "HANDOFF_ACCEPTED"


def test_coder_handoff_reject_keeps_original_loop(e2e: E2EFixture) -> None:
    fixture = e2e
    run_id = fixture.create_run()
    fixture.advance_until_executing(run_id)
    attempt = fixture.latest_attempt(run_id)
    fixture.seed_grant_for(attempt.attempt_id)
    fixture.client.queue_executions(attempt.attempt_id, [
        {"execution": {"actions": [{
            "request_id": "cap_write", "capability": "workspace.write",
            "parameters": {"path": "evidence.txt", "content": "e2e evidence"},
        }]}},
        {"execution": {"final_claim": "e2e execution complete"},
         "agent_actions": [_handoff_action("act_handoff_reject")]},
    ])
    # Drive to the review stage so the human can reject before the review.
    assert asyncio.run(fixture.orchestrator.advance(run_id))
    assert asyncio.run(fixture.orchestrator.advance(run_id))

    with fixture.sessions() as session:
        proposal = session.scalar(select(HandoffProposalRecord).where(
            HandoffProposalRecord.run_id == run_id,
        ))
        assert proposal is not None and proposal.status == HandoffStatus.PROPOSED.value
        proposal_id = proposal.proposal_id
    service = HandoffResolutionService(fixture.sessions, fixture.orchestrator)
    rejected = service.reject(
        proposal_id, actor_type="HUMAN", actor_id="operator",
        reason="the original coder can finish",
    )
    assert rejected.status is HandoffStatus.REJECTED

    snapshot = asyncio.run(fixture.orchestrator.run(run_id, max_steps=30))

    assert snapshot.state.value == ResearchRunState.COMPLETED.value
    assert (fixture.workspace_dir / "evidence.txt").read_text() == "e2e evidence"
    with fixture.sessions() as session:
        row = session.get(HandoffProposalRecord, proposal_id)
        assert row is not None
        assert row.status == HandoffStatus.REJECTED.value
        assert row.decision_actor_type == "HUMAN" and row.decision_actor_id == "operator"
        assert session.scalar(select(AuditEventRecord.event_type).where(
            AuditEventRecord.event_type == "HANDOFF_REJECTED",
        )) == "HANDOFF_REJECTED"
        # A rejected handoff creates no new Delegation, Attempt, or WorkOrder.
        assert session.scalar(select(AttemptRecord).where(
            AttemptRecord.attempt_id.like("att_handoff_%"),
        )) is None
        assert session.scalar(select(DelegationRecord).where(
            DelegationRecord.delegation_id.like("del_handoff_%"),
        )) is None
        assert session.scalar(select(WorkOrderRecord).where(
            WorkOrderRecord.work_order_id.like("wo_handoff_%"),
        )) is None


def _seed_console(tmp_path: Path) -> tuple[sessionmaker[Session], dict[str, Any]]:
    database = tmp_path / "console.db"
    migrate(database)
    sessions = session_factory(create_sqlite_engine(database))
    registry = AgentRegistryService(sessions)
    registry.register_profile(AgentProfile(
        agent_id=AgentId("agent_console_a"), display_name="Console Agent",
        roles=("executor",), trust_zone=AgentTrustZone.LOCAL_PRIVATE,
    ))
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_console_a"), agent_id=AgentId("agent_console_a"),
        adapter_kind=AgentAdapterKind.INTERNAL, runtime_name="console agent runtime",
    ))
    registry.acquire_runtime("runtime_console_a", owner_id="console-fixture", lease_seconds=3600)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(WorkspaceRecord(workspace_id="ws_con", name="console", version=1, created_at=now, updated_at=now))
        session.flush()
        session.add(ResearchRunRecord(
            run_id="run_con", workspace_id="ws_con", objective="concurrent consoles",
            state=ResearchRunState.ACTIVE.value, version=1, created_at=now, updated_at=now,
        ))
        session.flush()
        session.add(WorkOrderRecord(
            work_order_id="wo_con", run_id="run_con", objective="console execution",
            state=WorkOrderState.EXECUTING.value, idempotency_key="console-wo-0001",
            contract={
                "proposal_id": "wo_con", "objective": "console execution",
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
            delegation_id="del_con", run_id="run_con", work_order_id="wo_con",
            purpose=DelegationPurpose.EXECUTE.value, required_roles_json=["executor"],
            assigned_agent_id="agent_console_a", assigned_runtime_id="runtime_console_a",
            state="RUNNING", idempotency_key="console-del-0001", version=1,
            created_at=now, updated_at=now,
        ))
        session.flush()
        session.add(AttemptRecord(
            attempt_id="att_con", work_order_id="wo_con", delegation_id="del_con",
            state="RUNNING", terminal_at=None, version=1, created_at=now, updated_at=now,
        ))
        session.flush()
        runtime = session.get(AgentRuntimeRecord, "runtime_console_a")
        assert runtime is not None
        lease_id = cast(str, runtime.runtime_lease_id)
        session.add(AgentInvocationRecord(
            invocation_id="inv_con", delegation_id="del_con", run_id="run_con",
            work_order_id="wo_con", attempt_id="att_con",
            agent_id="agent_console_a", runtime_id="runtime_console_a",
            runtime_lease_id=lease_id,
            purpose=DelegationPurpose.EXECUTE.value, status="RUNNING",
            input_sha256="0" * 64, created_at=now,
        ))
        for event_id, event_type in (("evt_seed_1", "PLAN_CREATED"), ("evt_seed_2", "WORK_ORDER_CREATED")):
            session.add(AuditEventRecord(
                event_id=event_id, event_type=event_type, run_id="run_con",
                entity_type="work_order", entity_id="wo_con",
                actor_type="controller", actor_id="orchestrator",
                timestamp=now, correlation_id="run_con", causation_id=None, metadata_json={},
            ))
    state = {
        "lease_id": lease_id,
    }
    return sessions, state


def _append_event(sessions: sessionmaker[Session], event_id: str, event_type: str) -> None:
    with sessions.begin() as session:
        session.add(AuditEventRecord(
            event_id=event_id, event_type=event_type, run_id="run_con",
            entity_type="work_order", entity_id="wo_con",
            actor_type="controller", actor_id="orchestrator",
            timestamp=datetime.now(UTC), correlation_id="run_con",
            causation_id=None, metadata_json={},
        ))


def _sse_reader(
    index: int,
    base: str,
    stop_marker: str,
    received: list[list[str]],
    done: list[threading.Event],
) -> None:
    with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
        with client.stream("GET", f"{base}/api/runs/run_con/stream?follow=1") as response:
            assert response.status_code == 200
            for line in response.iter_lines():
                received[index].append(line)
                if stop_marker in line:
                    done[index].set()
                    return


def test_three_concurrent_consoles_survive_client_exit_and_sse_resumes(tmp_path: Path) -> None:
    sessions, state = _seed_console(tmp_path)
    api = LocalControlAPI(sessions)
    server = serve_local_control(api, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        host_text = host.decode("ascii") if isinstance(host, bytes) else host
        base = f"http://{host_text}:{port}"

        received: list[list[str]] = [[], [], []]
        done = [threading.Event() for _ in range(3)]
        readers = [
            threading.Thread(
                target=_sse_reader,
                args=(index, base, "evt_marker" if index == 0 else "evt_second", received, done),
                daemon=True,
            )
            for index in range(3)
        ]
        for reader in readers:
            reader.start()
        time.sleep(1.5)  # all three consoles are attached and replaying

        # While all three streams are live, the agent console stays read-only.
        status, payload = ControlResourceRouter(api).get("/api/agents/agent_console_a/console?run=run_con")
        assert status == 200 and isinstance(payload, dict)
        assert payload["agent"]["agent_id"] == "agent_console_a"
        assert [item["invocation_id"] for item in payload["invocations"]] == ["inv_con"]

        _append_event(sessions, "evt_marker", "HANDOFF_PROPOSED")
        assert done[0].wait(15), "first console missed the marker event"
        readers[0].join(timeout=5)
        assert not readers[0].is_alive()

        # Closing one client must not touch the Agent, its runtime lease, the
        # live invocation, or the run.
        with sessions() as session:
            runtime = session.get(AgentRuntimeRecord, "runtime_console_a")
            agent = session.get(AgentRecord, "agent_console_a")
            run = session.get(ResearchRunRecord, "run_con")
            invocation = session.get(AgentInvocationRecord, "inv_con")
            assert runtime is not None and runtime.runtime_lease_id == state["lease_id"]
            assert agent is not None and agent.enabled is True
            assert run is not None and run.state == ResearchRunState.ACTIVE.value
            assert invocation is not None and invocation.status == "RUNNING"

        marker_offset = max(item["stream_offset"] for item in api.events("run_con"))
        _append_event(sessions, "evt_second", "HANDOFF_ACCEPTED")
        assert done[1].wait(15) and done[2].wait(15), "surviving consoles missed the event"
        readers[1].join(timeout=5)
        readers[2].join(timeout=5)
        assert all("evt_marker" in "\n".join(items) for items in received)

        resumed = httpx.get(
            f"{base}/api/runs/run_con/stream",
            headers={"Last-Event-ID": str(marker_offset)},
            timeout=10,
        )
        assert resumed.status_code == 200
        assert resumed.headers["content-type"].startswith("text/event-stream")
        assert "evt_second" in resumed.text
        assert "evt_marker" not in resumed.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
