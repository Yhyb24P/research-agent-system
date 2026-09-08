"""PX04: managed multi-role turns — EXECUTE via controller capability flow,
PLAN/REVIEW structured returns, and fail-closed rejection of wrong shapes."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.agents.schemas import PlanProposal
from researchd.artifacts.provenance import ArtifactService
from researchd.artifacts.store import ContentAddressedArtifactStore
from researchd.collaboration.action_broker import AgentActionBroker
from researchd.collaboration.contracts import (
    AgentInvocationRequest,
    AgentProfile,
    AgentRuntime,
    ExecuteInvocationInput,
    PlanInvocationInput,
    ReviewInvocationInput,
)
from researchd.collaboration.gateway import CollaborationGateway
from researchd.collaboration.heterogeneous import (
    HttpxAgentClient,
    ManagedAgentTurnResponse,
    ManagedProcessAgentAdapter,
)
from researchd.collaboration.registry import AgentRegistryService
from researchd.collaboration.runtime import AgentAdapterCatalog
from researchd.context.builder import CloudContextSelection
from researchd.domain.enums import (
    AgentAdapterKind,
    AgentTrustZone,
    Capability,
    DelegationPurpose,
    InvocationStatus,
    ReviewDecisionKind,
)
from researchd.domain.ids import AgentId, AgentRuntimeId, DelegationId, InvocationId
from researchd.domain.review import ReviewDecision
from researchd.executor.capability_broker import CapabilityBroker
from researchd.executor.contracts import (
    CommandLimits,
    CommandResult,
    CommandSpec,
    ExecutorResult,
    GrantedWorkOrder,
    LocalAgentResponse,
    SandboxSpec,
)
from researchd.runtime_sessions.contracts import ProcessLaunchSpec
from researchd.runtime_sessions.launch_profiles import RuntimeLaunchProfileService
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    AgentInvocationRecord,
    AgentRuntimeRecord,
    AttemptRecord,
    DelegationRecord,
    ExecutionStepRecord,
    HandoffProposalRecord,
    ResearchRunRecord,
    RuntimeSessionRecord,
    WorkspaceGrantRecord,
    WorkspaceRecord,
    WorkspaceTransportRecord,
    WorkOrderRecord,
)
from tests.integration.test_storage import migrate

ModelT = TypeVar("ModelT")


def test_managed_http_read_bound_is_generic() -> None:
    client = HttpxAgentClient(read_timeout_seconds=42.0)
    assert client.read_timeout_seconds == 42.0


def _get(session: Session, model: type[ModelT], primary_key: str) -> ModelT:
    row = session.get(model, primary_key)
    assert row is not None
    return row


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


class ScriptedClient:
    """HttpAgentClient double: returns queued ManagedAgentTurnResponse payloads."""

    def __init__(self) -> None:
        self.responses: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []

    def queue(self, response: dict[str, Any]) -> None:
        self.responses.append(response)

    async def invoke(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        del endpoint
        self.requests.append(payload)
        return self.responses.pop(0)


class ManagedFixture:
    def __init__(self, tmp_path: Path) -> None:
        database = tmp_path / "managed.db"
        migrate(database)
        self.sessions: sessionmaker[Session] = session_factory(create_sqlite_engine(database))
        self.workspace_dir = tmp_path / "managed-workspace"
        self.workspace_dir.mkdir()
        registry = AgentRegistryService(self.sessions)
        registry.register_profile(AgentProfile(
            agent_id=AgentId("agent_managed"), display_name="Managed",
            roles=("planner", "executor", "reviewer"),
            trust_zone=AgentTrustZone.LOCAL_PRIVATE,
        ))
        registry.register_runtime(AgentRuntime(
            runtime_id=AgentRuntimeId("runtime_managed"), agent_id=AgentId("agent_managed"),
            adapter_kind=AgentAdapterKind.PROCESS, runtime_name="managed process",
            endpoint_ref="http://127.0.0.1:9100/turn",
        ))
        self.launch_profiles = RuntimeLaunchProfileService(self.sessions, registry)
        self.launch_profiles.register_process(
            "runtime_managed", ProcessLaunchSpec(argv=("/usr/bin/true",), cwd="/tmp"),
        )
        registry.acquire_runtime("runtime_managed", owner_id="managed-turn-test")
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            session.add(WorkspaceRecord(
                workspace_id="ws_managed", name="managed", version=1,
                created_at=now, updated_at=now,
            ))
            session.flush()
            session.add(ResearchRunRecord(
                run_id="run_managed", workspace_id="ws_managed", objective="managed turns",
                state="ACTIVE", version=1, created_at=now, updated_at=now,
            ))
            session.flush()
            session.add(WorkOrderRecord(
                work_order_id="wo_managed", run_id="run_managed", objective="managed execution",
                state="EXECUTING", idempotency_key="managed-wo-0001",
                contract={
                    "proposal_id": "wo_managed", "objective": "managed execution",
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
                delegation_id="del_managed", run_id="run_managed", work_order_id="wo_managed",
                purpose=DelegationPurpose.EXECUTE.value, required_roles_json=["executor"],
                assigned_agent_id="agent_managed", assigned_runtime_id="runtime_managed",
                state="RUNNING", idempotency_key="managed-del-0001", version=1,
                created_at=now, updated_at=now,
            ))
            session.flush()
            session.add(AttemptRecord(
                attempt_id="att_managed", work_order_id="wo_managed",
                delegation_id="del_managed", state="RUNNING", terminal_at=None,
                version=1, created_at=now, updated_at=now,
            ))
            session.flush()
            session.add(WorkspaceGrantRecord(
                workspace_grant_id="grant_managed", delegation_id="del_managed",
                source_workspace_id="ws_managed", access_mode="READ_WRITE",
                allowed_paths=["."], excluded_paths=[], classification_ceiling="LOCAL_ONLY",
                max_total_bytes=10_000_000, max_file_count=1_000,
                max_single_file_bytes=5_000_000, lease_seconds=3600,
                lease_started_at=now, lease_expires_at=now + timedelta(hours=1),
                renewal_policy="DENY", transport_kind="ARCHIVE",
                reconciliation_mode="ARTIFACT_ONLY", state="ACTIVE",
                cleanup_state="PENDING", version=1, created_at=now, updated_at=now,
            ))
            session.flush()
            session.add(WorkspaceTransportRecord(
                workspace_transport_id="wst_managed", workspace_grant_id="grant_managed",
                transport_kind="ARCHIVE", transport_handle={"root": str(self.workspace_dir)},
                remote_workspace_handle=str(self.workspace_dir), state="ACTIVE",
                created_at=now,
            ))
            session.flush()
            runtime = session.get(AgentRuntimeRecord, "runtime_managed")
            assert runtime is not None
            session.add(RuntimeSessionRecord(
                runtime_session_id="rs_managed", runtime_id="runtime_managed",
                launch_mode="PROCESS", supervisor_state="HEALTHY",
                launch_spec_json={"argv": ["/usr/bin/true"], "cwd": "/tmp"},
                launch_profile_sha256=self.launch_profiles.get("runtime_managed").spec_sha256,
                reattach_state="NOT_APPLICABLE", started_at=now, last_health_at=now,
                version=1, created_at=now, updated_at=now,
            ))
            session.add(AgentInvocationRecord(
                invocation_id="inv_managed", delegation_id="del_managed",
                run_id="run_managed", work_order_id="wo_managed",
                attempt_id="att_managed", workspace_grant_id="grant_managed",
                agent_id="agent_managed", runtime_id="runtime_managed",
                runtime_lease_id=runtime.runtime_lease_id,
                purpose=DelegationPurpose.EXECUTE.value, status="RUNNING",
                input_sha256="0" * 64, created_at=now,
            ))
        self.backend = FakeSandboxBackend()
        self.client = ScriptedClient()
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
            planning_capabilities=frozenset({Capability.SANDBOX_SHELL}),
        )

    def execute_request(self, *, granted: frozenset[Capability] = frozenset({Capability.WORKSPACE_WRITE}),
                       grant_id: str | None = "grant_managed") -> AgentInvocationRequest:
        return AgentInvocationRequest(
            invocation_id=InvocationId("inv_managed"),
            delegation_id=DelegationId("del_managed"),
            run_id="run_managed", work_order_id="wo_managed", attempt_id="att_managed",
            workspace_grant_id=grant_id,
            agent_id=AgentId("agent_managed"), runtime_id=AgentRuntimeId("runtime_managed"),
            purpose=DelegationPurpose.EXECUTE, input_sha256="0" * 64,
            typed_input=ExecuteInvocationInput(work_order=GrantedWorkOrder(
                attempt_id="att_managed", objective="managed execution",
                granted_capabilities=granted,
                sandbox=SandboxSpec(attempt_id="att_managed", workspace="/workspace"),
            )),
        )

    def plan_request(self) -> AgentInvocationRequest:
        return AgentInvocationRequest(
            invocation_id=InvocationId("inv_managed"),
            delegation_id=DelegationId("del_managed"),
            run_id="run_managed",
            agent_id=AgentId("agent_managed"), runtime_id=AgentRuntimeId("runtime_managed"),
            purpose=DelegationPurpose.PLAN, input_sha256="1" * 64,
            typed_input=PlanInvocationInput(context=CloudContextSelection(run_id="run_managed")),
        )

    def review_request(self) -> AgentInvocationRequest:
        return AgentInvocationRequest(
            invocation_id=InvocationId("inv_managed"),
            delegation_id=DelegationId("del_managed"),
            run_id="run_managed", work_order_id="wo_managed",
            agent_id=AgentId("agent_managed"), runtime_id=AgentRuntimeId("runtime_managed"),
            purpose=DelegationPurpose.REVIEW, input_sha256="2" * 64,
            typed_input=ReviewInvocationInput(context=CloudContextSelection(run_id="run_managed")),
        )


@pytest.fixture
def managed(tmp_path: Path) -> ManagedFixture:
    return ManagedFixture(tmp_path)


def test_execute_turn_runs_through_controller_capability_flow(managed: ManagedFixture) -> None:
    managed.client.queue({"execution": {"actions": [{
        "request_id": "cap_write", "capability": "workspace.write",
        "parameters": {"path": "notes.txt", "content": "evidence"},
    }]}})
    managed.client.queue({
        "execution": {"final_claim": "managed execution complete"},
        "agent_actions": [{
            "kind": "handoff", "action_id": "act_managed",
            "requested_mode": "CONTINUE", "reason": "continue with managed agent",
            "proposed_target_agent_id": "agent_managed",
        }],
    })

    result = asyncio.run(managed.adapter.invoke(managed.execute_request()))

    assert result.status is InvocationStatus.SUCCEEDED
    assert result.output_type == "ExecutorResult"
    assert result.output is not None
    executor_result = ExecutorResult.model_validate(result.output)
    assert executor_result.status == "execution_complete"
    assert executor_result.capability_results[0].status == "ok"
    assert executor_result.reported_claims == ("managed execution complete",)
    assert (managed.workspace_dir / "notes.txt").read_text() == "evidence"
    assert managed.backend.commands == []
    with managed.sessions() as session:
        step = session.get(ExecutionStepRecord, "cap_write")
        assert step is not None and step.status == "COMPLETED"
        handoff = session.scalar(select(HandoffProposalRecord).where(
            HandoffProposalRecord.source_invocation_id == "inv_managed",
        ))
        assert handoff is not None and handoff.status == "PROPOSED"
    payload = managed.client.requests[0]
    assert payload["purpose"] == "EXECUTE" and payload["attempt_id"] == "att_managed"
    assert payload["payload"]["granted_capabilities"] == ["workspace.write"]


def test_ungranted_capability_is_denied_by_broker(managed: ManagedFixture) -> None:
    managed.client.queue({"execution": {"actions": [{
        "request_id": "cap_push", "capability": "git.push", "parameters": {},
    }]}})
    managed.client.queue({"execution": {"final_claim": "push was denied"}})

    result = asyncio.run(managed.adapter.invoke(managed.execute_request(granted=frozenset())))

    # The ungranted capability is denied by the controller broker and never
    # reaches the sandbox; the turn itself completes with the denial recorded.
    assert result.status is InvocationStatus.SUCCEEDED
    assert result.output is not None
    executor_result = ExecutorResult.model_validate(result.output)
    assert executor_result.capability_results[0].status == "denied"
    assert executor_result.capability_results[0].reason_code == "CAPABILITY_NOT_GRANTED"
    assert managed.backend.commands == []


def test_execute_turn_without_execution_result_fails_closed(managed: ManagedFixture) -> None:
    managed.client.queue({"output": {"note": "business payload on an execution turn"}})

    result = asyncio.run(managed.adapter.invoke(managed.execute_request()))

    assert result.status is InvocationStatus.FAILED
    assert result.reason_code == "model_unavailable"
    assert result.output is not None
    assert ExecutorResult.model_validate(result.output).status == "model_unavailable"


def test_execute_turn_requires_active_workspace_grant(managed: ManagedFixture) -> None:
    with managed.sessions.begin() as session:
        _get(session, WorkspaceGrantRecord, "grant_managed").state = "EXPIRED"
    managed.client.queue({"execution": {"final_claim": "should not run"}})

    result = asyncio.run(managed.adapter.invoke(managed.execute_request()))

    assert result.status is InvocationStatus.FAILED
    assert result.reason_code == "WORKSPACE_GRANT_UNAVAILABLE"
    assert managed.client.requests == []

    managed.client.queue({"execution": {"final_claim": "no grant binding"}})
    unbound = asyncio.run(managed.adapter.invoke(managed.execute_request(grant_id=None)))
    assert unbound.status is InvocationStatus.FAILED
    assert unbound.reason_code == "WORKSPACE_GRANT_UNAVAILABLE"


def test_execute_turn_requires_healthy_supervised_session(managed: ManagedFixture) -> None:
    with managed.sessions.begin() as session:
        _get(session, RuntimeSessionRecord, "rs_managed").supervisor_state = "STOPPED"
    managed.client.queue({"execution": {"final_claim": "should not run"}})

    result = asyncio.run(managed.adapter.invoke(managed.execute_request()))

    assert result.status is InvocationStatus.FAILED
    assert result.reason_code == "PROCESS_RUNTIME_UNAVAILABLE"
    assert managed.client.requests == []
    health = asyncio.run(managed.adapter.health(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_managed"), agent_id=AgentId("agent_managed"),
        adapter_kind=AgentAdapterKind.PROCESS, runtime_name="managed process",
    )))
    assert health.healthy is False


def test_plan_turn_output_validates_as_plan_proposal(managed: ManagedFixture) -> None:
    managed.client.queue({"output": {
        "proposal_id": "plan_managed",
        "hypotheses": [{"hypothesis_id": "h1", "statement": "managed plan", "priority": 1}],
        "proposed_work_orders": [], "risks": [], "required_evidence": [],
    }})

    result = asyncio.run(managed.adapter.invoke(managed.plan_request()))

    assert result.status is InvocationStatus.SUCCEEDED
    assert result.output_type == "PLAN"
    assert result.output is not None
    plan = PlanProposal.model_validate(result.output)
    assert plan.proposal_id == "plan_managed"
    assert managed.client.requests[0]["purpose"] == "PLAN"
    assert managed.client.requests[0]["allowed_capabilities"] == ["sandbox.shell"]


def test_review_turn_output_validates_as_review_decision(managed: ManagedFixture) -> None:
    managed.client.queue({"output": {
        "decision": "HUMAN_REQUIRED", "work_order_id": "wo_managed",
        "evidence_refs": ["obs_managed"], "deficiencies": [],
        "rationale": "needs a human reviewer", "requested_evidence": [],
    }})

    result = asyncio.run(managed.adapter.invoke(managed.review_request()))

    assert result.status is InvocationStatus.SUCCEEDED
    assert result.output_type == "REVIEW"
    assert result.output is not None
    decision = ReviewDecision.model_validate(result.output)
    assert decision.decision is ReviewDecisionKind.HUMAN_REQUIRED
    assert managed.client.requests[0]["purpose"] == "REVIEW"


def test_business_turn_without_structured_output_fails_closed(managed: ManagedFixture) -> None:
    managed.client.queue({"execution": {"final_claim": "execution payload on a plan turn"}})

    result = asyncio.run(managed.adapter.invoke(managed.plan_request()))

    assert result.status is InvocationStatus.FAILED
    assert result.reason_code == "MANAGED_TURN_ValueError"


def test_typed_input_kind_must_match_purpose() -> None:
    execute_input = ExecuteInvocationInput(work_order=GrantedWorkOrder(
        attempt_id="att_x", objective="x", granted_capabilities=frozenset(),
        sandbox=SandboxSpec(attempt_id="att_x", workspace="/workspace"),
    ))
    with pytest.raises(ValidationError, match="typed invocation input kind must match purpose"):
        AgentInvocationRequest(
            invocation_id=InvocationId("inv_x"), delegation_id=DelegationId("del_x"),
            run_id="run_x", agent_id=AgentId("agent_x"), runtime_id=AgentRuntimeId("runtime_x"),
            purpose=DelegationPurpose.PLAN, input_sha256="0" * 64, typed_input=execute_input,
        )
    with pytest.raises(ValidationError, match="typed invocation input kind must match purpose"):
        AgentInvocationRequest(
            invocation_id=InvocationId("inv_y"), delegation_id=DelegationId("del_y"),
            run_id="run_y", agent_id=AgentId("agent_x"), runtime_id=AgentRuntimeId("runtime_x"),
            purpose=DelegationPurpose.EXECUTE, input_sha256="1" * 64,
            typed_input=PlanInvocationInput(context=CloudContextSelection(run_id="run_y")),
        )


def test_managed_turn_response_requires_exactly_one_result_kind() -> None:
    with pytest.raises(ValidationError, match="exactly one result kind"):
        ManagedAgentTurnResponse(
            execution=LocalAgentResponse(final_claim="both"),
            output={"proposal_id": "both"},
        )
    with pytest.raises(ValidationError, match="exactly one result kind"):
        ManagedAgentTurnResponse()


def test_gateway_routes_plan_and_review_through_managed_runtime(managed: ManagedFixture) -> None:
    from researchd.collaboration.delegation import DelegationService
    from researchd.collaboration.invocation import InvocationService

    catalog = AgentAdapterCatalog(managed.sessions)
    catalog.register(AgentAdapterKind.PROCESS, managed.adapter)
    gateway = CollaborationGateway(
        None, None,
        delegations=DelegationService(managed.sessions),
        invocations=InvocationService(managed.sessions),
        agent_id=AgentId("agent_managed"), runtime_id=AgentRuntimeId("runtime_managed"),
        catalog=catalog,
    )

    managed.client.queue({"output": {
        "proposal_id": "plan_gateway", "hypotheses": [], "proposed_work_orders": [],
        "risks": [], "required_evidence": [],
    }})
    plan = asyncio.run(gateway.plan(CloudContextSelection(run_id="run_managed")))
    assert plan.output.proposal_id == "plan_gateway"

    managed.client.queue({"output": {
        "decision": "ACCEPT", "work_order_id": "wo_managed",
        "evidence_refs": ["obs_managed"], "deficiencies": [],
        "rationale": "looks complete", "requested_evidence": [],
    }})
    review = asyncio.run(gateway.review(CloudContextSelection(run_id="run_managed")))
    assert review.output.decision is ReviewDecisionKind.ACCEPT

    with managed.sessions() as session:
        plan_invocation = session.scalar(select(AgentInvocationRecord).where(
            AgentInvocationRecord.purpose == "PLAN",
        ))
        assert plan_invocation is not None and plan_invocation.status == "SUCCEEDED"
        assert plan_invocation.output_type == "PlanProposal"
