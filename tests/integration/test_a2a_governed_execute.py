"""PX07-01: governed A2A execution boundary security matrix.

Covers the five frozen matrix classes from 推进计划.md:
1. registry/endpoint forgery rejection,
2. egress redaction of the V2 remote-execution envelope,
3. no workspace/capability/token leakage,
4. lease/cancel scoping,
5. mixed PROCESS+A2A end-to-end with purpose-based adapter-kind limits.
"""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.adapters.a2a import (
    EXECUTOR_RESULT_MEDIA_TYPE,
    REMOTE_EXECUTION_REQUEST_MEDIA_TYPE,
    RemoteExecutionRequest,
)
from researchd.artifacts.provenance import ArtifactService
from researchd.artifacts.store import ContentAddressedArtifactStore
from researchd.collaboration.action_broker import AgentActionBroker
from researchd.collaboration.contracts import (
    AgentProfile,
    AgentRuntime,
    AgentInvocationRequest,
    Delegation,
    ExecuteInvocationInput,
    PlanInvocationInput,
    ReviewInvocationInput,
)
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.gateway import CollaborationGateway
from researchd.collaboration.heterogeneous import (
    GovernedA2ARemoteAgentAdapter,
    ManagedProcessAgentAdapter,
)
from researchd.collaboration.invocation import InvocationService
from researchd.collaboration.registry import AgentRegistryService
from researchd.collaboration.runtime import AgentAdapterCatalog
from researchd.collaboration.selector import AgentSelector
from researchd.context.agent_context import AgentContextBuilder, AgentContextBundle
from researchd.context.builder import CloudContextSelection, ContextBuilder
from researchd.context.cloud_bundle import CloudContextBundle
from researchd.context.redaction import DeterministicRedactor
from researchd.domain.enums import (
    AgentAdapterKind,
    AgentTrustZone,
    Capability,
    DelegationPurpose,
    InvocationStatus,
    ResearchRunState,
    WorkOrderState,
)
from researchd.domain.ids import AgentId, AgentRuntimeId, DelegationId, InvocationId
from researchd.executor.capability_broker import CapabilityBroker
from researchd.executor.contracts import (
    CommandLimits,
    CommandResult,
    CommandSpec,
    GrantedWorkOrder,
    SandboxSpec,
)
from researchd.orchestrator.engine import OrchestrationError, OrchestrationLimits, ResearchOrchestrator
from researchd.policy.engine import BudgetLimits, DeterministicPolicyEngine, RecordingPolicyEngine
from researchd.runtime_sessions.contracts import ProcessLaunchSpec
from researchd.runtime_sessions.launch_profiles import RuntimeLaunchProfileService
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    AgentInvocationRecord,
    AgentRuntimeRecord,
    AuditEventRecord,
    AttemptRecord,
    DelegationRecord,
    ResearchRunRecord,
    RuntimeSessionRecord,
    WorkspaceGrantRecord,
    WorkspaceRecord,
    WorkspaceTransportRecord,
    WorkOrderRecord,
)
from tests.integration.test_e2e_concurrency import _plan_output
from tests.integration.test_orchestrator import FakeVerifier, _proposal, make_orchestrator
from tests.integration.test_storage import migrate

LOOPBACK_ENDPOINT = "http://127.0.0.1:8787/a2a"
ROTATED_ENDPOINT = "http://127.0.0.1:9999/a2a"
TENANT = "tenant-gov"
MANAGED_ENDPOINT = "http://127.0.0.1:9100/turn"

UNSAFE_ENDPOINTS = (
    "http://10.1.1.1:8787/a2a",
    "https://user:pass@example.com/a2a",
    "https://example.com/a2a?tenant=x",
    "https://example.com/a2a#frag",
)


class RecordingA2AClient:
    """A2A client double serving the V2 remote-execution envelope."""

    def __init__(self, *, capability_results: list[dict[str, Any]] | None = None) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.endpoints: list[str] = []
        self.cancelled: list[tuple[str, str | None]] = []
        self.capability_results = capability_results or []

    def bind_endpoint(self, endpoint: str) -> "RecordingA2AClient":
        self.endpoints.append(endpoint)
        return self

    async def send(
        self,
        payload: dict[str, Any],
        *,
        on_task: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        self.payloads.append(payload)
        data = payload["message"]["parts"][0]["data"]
        attempt_id = data["attempt_id"]
        response = {
            "id": f"task_{payload['message']['messageId']}",
            "contextId": payload["message"]["contextId"],
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{
                "artifactId": f"result_{attempt_id}",
                "parts": [{
                    "data": {
                        "attempt_id": attempt_id,
                        "status": "execution_complete",
                        "capability_results": list(self.capability_results),
                        "reported_claims": ["remote ok"],
                        "errors": [],
                    },
                    "mediaType": EXECUTOR_RESULT_MEDIA_TYPE,
                }],
            }],
            "history": [payload["message"]],
            "metadata": payload.get("metadata", {}),
        }
        if on_task is not None:
            on_task(response)
        return response

    async def cancel(self, *, task_id: str, tenant: str | None = None) -> dict[str, Any]:
        self.cancelled.append((task_id, tenant))
        return {
            "id": task_id,
            "contextId": self.payloads[-1]["message"]["contextId"] if self.payloads else "ctx_cancel",
            "status": {"state": "TASK_STATE_CANCELED"},
            "artifacts": [],
            "history": [],
        }


class FakeSandboxBackend:
    def run(self, sandbox: SandboxSpec, command: CommandSpec) -> CommandResult:
        del sandbox
        return CommandResult(
            execution_id=command.execution_id, exit_code=0, stdout=b"ok",
            stderr=b"", timed_out=False, cancelled=False,
            output_limit_exceeded=False, duration_seconds=0.1,
        )

    def cancel(self, execution_id: str) -> bool:
        del execution_id
        return False


def _bundle(
    fixture: "DirectFixture",
    *,
    target_agent_id: str = "agent_a2a",
    target_runtime_id: str = "runtime_a2a",
    run_id: str | None = None,
    work_order_id: str | None = None,
) -> AgentContextBundle:
    zone = AgentTrustZone.REMOTE_PRIVATE
    context = CloudContextBundle(
        run_id=run_id or fixture.run_id,
        work_order_id=work_order_id if work_order_id is not None else fixture.order.work_order_id,
        goal="governed a2a execution",
        objective="fix the reproducible NaN smoke failure",
        selected_artifacts=(),
    )
    return AgentContextBundle(
        target_agent_id=target_agent_id,
        target_runtime_id=target_runtime_id,
        target_trust_zone=zone,
        purpose=DelegationPurpose.EXECUTE,
        run_id=run_id or fixture.run_id,
        work_order_id=work_order_id if work_order_id is not None else fixture.order.work_order_id,
        selected_context=context,
        bundle_sha256="c" * 64,
        policy=AgentContextBuilder.policy_for(zone),
    )


class DirectFixture:
    """Registry-seeded fixture driving GovernedA2ARemoteAgentAdapter directly."""

    def __init__(self, tmp_path: Path) -> None:
        self.sessions, self.orchestrator, _, _ = make_orchestrator(tmp_path, cloud_responses=[_proposal()])
        self.run_id = self.orchestrator.create_run(workspace_id="ws_e2e", objective="governed a2a execute")
        for _ in range(4):
            asyncio.run(self.orchestrator.advance(self.run_id))
        with self.sessions() as session:
            self.order = session.query(WorkOrderRecord).one()
        self.registry = AgentRegistryService(self.sessions)
        self.registry.register_profile(AgentProfile(
            agent_id=AgentId("agent_a2a"), display_name="Governed remote executor",
            roles=("executor",), trust_zone=AgentTrustZone.REMOTE_PRIVATE,
        ))
        self.registry.register_runtime(AgentRuntime(
            runtime_id=AgentRuntimeId("runtime_a2a"), agent_id=AgentId("agent_a2a"),
            adapter_kind=AgentAdapterKind.A2A, runtime_name="governed A2A",
            endpoint_ref=LOOPBACK_ENDPOINT, protocols=("1.0",),
            metadata={"a2a_tenant": TENANT},
        ))
        self.lease = self.registry.acquire_runtime("runtime_a2a", owner_id="gov-direct", lease_seconds=3600)
        delegations = DelegationService(self.sessions)
        delegations.create(Delegation(
            delegation_id=DelegationId("del_gov_a2a"), run_id=self.run_id,
            work_order_id=self.order.work_order_id, purpose=DelegationPurpose.EXECUTE,
            idempotency_key="del-gov-a2a",
        ))
        delegations.assign("del_gov_a2a", agent_id="agent_a2a", runtime_id="runtime_a2a")
        now = datetime.now(UTC)
        self.attempt = AttemptRecord(
            attempt_id="att_gov_a2a",
            work_order_id=self.order.work_order_id,
            delegation_id="del_gov_a2a",
            state="RUNNING",
            terminal_at=None,
            version=1,
            created_at=now,
            updated_at=now,
        )
        with self.sessions.begin() as session:
            session.add(self.attempt)
        self.client = RecordingA2AClient()
        self.adapter = GovernedA2ARemoteAgentAdapter(
            self.sessions, client_factory=self.client.bind_endpoint,
        )

    def rotate(self, *, endpoint_ref: str | None = None, protocols: tuple[str, ...] | None = None,
               metadata: dict[str, str] | None = None) -> None:
        runtime = self.registry.get_runtime("runtime_a2a")
        self.registry.update_runtime(AgentRuntime(
            runtime_id=runtime.runtime_id, agent_id=runtime.agent_id,
            adapter_kind=AgentAdapterKind.A2A, runtime_name=runtime.runtime_name,
            endpoint_ref=endpoint_ref if endpoint_ref is not None else runtime.endpoint_ref,
            protocols=protocols if protocols is not None else runtime.protocols,
            metadata=metadata if metadata is not None else dict(runtime.metadata),
        ))


class _Unsentinel:
    pass


_UNSET_BUNDLE = _Unsentinel()


def _execute_request(
    fixture: DirectFixture,
    *,
    invocation_id: str = "inv_gov_a2a",
    bundle: AgentContextBundle | None | _Unsentinel = _UNSET_BUNDLE,
    attempt_id: str | None = "att_gov_a2a",
    endpoint_ref: str | None = None,
) -> AgentInvocationRequest:
    if isinstance(bundle, _Unsentinel):
        bundle = _bundle(fixture)
    effective_attempt = attempt_id if attempt_id is not None else fixture.attempt.attempt_id
    return AgentInvocationRequest(
        invocation_id=InvocationId(invocation_id),
        delegation_id=DelegationId("del_gov_a2a"),
        run_id=fixture.run_id,
        work_order_id=fixture.order.work_order_id,
        attempt_id=attempt_id,
        agent_id=AgentId("agent_a2a"),
        runtime_id=AgentRuntimeId("runtime_a2a"),
        purpose=DelegationPurpose.EXECUTE,
        input_sha256="g" * 64,
        endpoint_ref=endpoint_ref,
        context_bundle=bundle,
        typed_input=ExecuteInvocationInput(work_order=GrantedWorkOrder(
            attempt_id=effective_attempt,
            objective=fixture.order.objective,
            granted_capabilities=frozenset(),
            sandbox=SandboxSpec(attempt_id=effective_attempt, workspace="/workspace"),
        )),
    )


def _plan_request(fixture: DirectFixture) -> AgentInvocationRequest:
    return AgentInvocationRequest(
        invocation_id=InvocationId("inv_gov_plan"),
        delegation_id=DelegationId("del_gov_a2a"),
        run_id=fixture.run_id,
        agent_id=AgentId("agent_a2a"),
        runtime_id=AgentRuntimeId("runtime_a2a"),
        purpose=DelegationPurpose.PLAN,
        input_sha256="p" * 64,
        typed_input=PlanInvocationInput(context=CloudContextSelection(run_id=fixture.run_id)),
    )


def _review_request(fixture: DirectFixture) -> AgentInvocationRequest:
    return AgentInvocationRequest(
        invocation_id=InvocationId("inv_gov_review"),
        delegation_id=DelegationId("del_gov_a2a"),
        run_id=fixture.run_id,
        work_order_id=fixture.order.work_order_id,
        agent_id=AgentId("agent_a2a"),
        runtime_id=AgentRuntimeId("runtime_a2a"),
        purpose=DelegationPurpose.REVIEW,
        input_sha256="r" * 64,
        typed_input=ReviewInvocationInput(context=CloudContextSelection(
            run_id=fixture.run_id, work_order_id=fixture.order.work_order_id,
        )),
    )


def _invoke(fixture: DirectFixture, invocation_id: str = "inv_gov_a2a") -> Any:
    request = _execute_request(fixture, invocation_id=invocation_id)
    InvocationService(fixture.sessions).start(request)
    return asyncio.run(fixture.adapter.invoke(request))


@pytest.fixture
def fixture(tmp_path: Path) -> DirectFixture:
    return DirectFixture(tmp_path)


def test_governed_a2a_rejects_non_execute_purposes_and_incomplete_scope(fixture: DirectFixture) -> None:
    for request in (
        _plan_request(fixture),
        _review_request(fixture),
        _execute_request(fixture, invocation_id="inv_gov_no_attempt", attempt_id=None),
        _execute_request(fixture, invocation_id="inv_gov_no_bundle", bundle=None),
    ):
        result = asyncio.run(fixture.adapter.invoke(request))
        assert result.status is InvocationStatus.FAILED
        assert result.reason_code == "A2A_GOVERNED_SCOPE_REQUIRED"
    assert fixture.client.payloads == []


def test_governed_a2a_rejects_disabled_unleased_or_expired_runtime(fixture: DirectFixture) -> None:
    for suffix in ("disabled", "agent_disabled", "unleased", "expired"):
        InvocationService(fixture.sessions).start(
            _execute_request(fixture, invocation_id=f"inv_gov_{suffix}"),
        )
    fixture.registry.set_runtime_enabled("runtime_a2a", False)
    result = asyncio.run(fixture.adapter.invoke(_execute_request(fixture, invocation_id="inv_gov_disabled")))
    assert result.status is InvocationStatus.FAILED and result.reason_code == "A2A_GOVERNED_ValueError"
    fixture.registry.set_runtime_enabled("runtime_a2a", True)

    fixture.registry.disable("agent_a2a")
    result = asyncio.run(fixture.adapter.invoke(_execute_request(fixture, invocation_id="inv_gov_agent_disabled")))
    assert result.status is InvocationStatus.FAILED and result.reason_code == "A2A_GOVERNED_ValueError"
    fixture.registry.enable("agent_a2a")

    fixture.registry.release_runtime(fixture.lease)
    result = asyncio.run(fixture.adapter.invoke(_execute_request(fixture, invocation_id="inv_gov_unleased")))
    assert result.status is InvocationStatus.FAILED and result.reason_code == "A2A_GOVERNED_ValueError"
    fixture.lease = fixture.registry.acquire_runtime("runtime_a2a", owner_id="gov-direct", lease_seconds=3600)

    with fixture.sessions.begin() as session:
        row = session.get(AgentRuntimeRecord, "runtime_a2a")
        assert row is not None
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    result = asyncio.run(fixture.adapter.invoke(_execute_request(fixture, invocation_id="inv_gov_expired")))
    assert result.status is InvocationStatus.FAILED and result.reason_code == "A2A_GOVERNED_ValueError"
    assert fixture.client.payloads == []


@pytest.mark.parametrize("endpoint", UNSAFE_ENDPOINTS)
def test_governed_a2a_rejects_unsafe_endpoint(fixture: DirectFixture, endpoint: str) -> None:
    fixture.rotate(endpoint_ref=endpoint)
    result = _invoke(fixture, "inv_gov_unsafe_endpoint")
    assert result.status is InvocationStatus.FAILED and result.reason_code == "A2A_GOVERNED_ValueError"
    assert fixture.client.payloads == []


def test_governed_a2a_requires_declared_protocol_and_valid_tenant(fixture: DirectFixture) -> None:
    fixture.rotate(protocols=())
    assert _invoke(fixture, "inv_gov_no_protocol").reason_code == "A2A_GOVERNED_ValueError"
    fixture.rotate(protocols=("1.0",), metadata={"a2a_tenant": "bad\x01tenant"})
    assert _invoke(fixture, "inv_gov_bad_tenant_ctl").reason_code == "A2A_GOVERNED_ValueError"
    fixture.rotate(metadata={"a2a_tenant": "x" * 200})
    assert _invoke(fixture, "inv_gov_bad_tenant_long").reason_code == "A2A_GOVERNED_ValueError"
    fixture.rotate(metadata={"a2a_tenant": ""})
    assert _invoke(fixture, "inv_gov_bad_tenant_empty").reason_code == "A2A_GOVERNED_ValueError"
    fixture.rotate(metadata={"a2a_tenant": TENANT})
    assert _invoke(fixture, "inv_gov_tenant_restored").status is InvocationStatus.SUCCEEDED


def test_governed_a2a_context_bundle_scope_mismatch_fails_closed(fixture: DirectFixture) -> None:
    for bundle in (
        _bundle(fixture, target_agent_id="agent_other"),
        _bundle(fixture, target_runtime_id="runtime_other"),
        _bundle(fixture, run_id="run_other"),
        _bundle(fixture, work_order_id="wo_other"),
    ):
        result = asyncio.run(fixture.adapter.invoke(_execute_request(
            fixture, invocation_id="inv_gov_scope", bundle=bundle,
        )))
        assert result.status is InvocationStatus.FAILED and result.reason_code == "A2A_GOVERNED_ValueError"
    assert fixture.client.payloads == []


def test_governed_a2a_re_resolves_registry_on_every_call_and_ignores_request_endpoint(fixture: DirectFixture) -> None:
    request = _execute_request(fixture, invocation_id="inv_gov_override", endpoint_ref="http://evil.example/a2a")
    InvocationService(fixture.sessions).start(request)
    result = asyncio.run(fixture.adapter.invoke(request))
    assert result.status is InvocationStatus.SUCCEEDED
    assert fixture.client.endpoints == [LOOPBACK_ENDPOINT]
    fixture.rotate(endpoint_ref=ROTATED_ENDPOINT)
    assert _invoke(fixture, "inv_gov_rotated").status is InvocationStatus.SUCCEEDED
    assert fixture.client.endpoints == [LOOPBACK_ENDPOINT, ROTATED_ENDPOINT]


def test_governed_a2a_envelope_is_scope_bound_and_redacted(fixture: DirectFixture) -> None:
    result = _invoke(fixture, "inv_gov_egress")
    assert result.status is InvocationStatus.SUCCEEDED
    payload = fixture.client.payloads[0]
    part = payload["message"]["parts"][0]
    assert part["mediaType"] == REMOTE_EXECUTION_REQUEST_MEDIA_TYPE
    data = part["data"]
    assert set(data) == {
        "invocation_id", "run_id", "work_order_id", "attempt_id", "objective", "context",
    }
    assert data["invocation_id"] == "inv_gov_egress"
    assert data["run_id"] == fixture.run_id
    assert data["work_order_id"] == fixture.order.work_order_id
    assert data["attempt_id"] == "att_gov_a2a"
    context = data["context"]
    assert context["target_agent_id"] == "agent_a2a"
    assert context["target_runtime_id"] == "runtime_a2a"
    assert context["target_trust_zone"] == "REMOTE_PRIVATE"
    assert set(context["policy"]["allowed_classifications"]) == {"PUBLIC", "CLOUD_SAFE", "PROJECT_PRIVATE"}
    assert context["selected_context"]["selected_artifacts"] == []
    serialized = json.dumps(payload, sort_keys=True)
    for forbidden in (
        "/workspace", "workspace_grant", "sandbox", "granted_capabilities",
        "capability", "token", "credential", "lease", "grant_",
    ):
        assert forbidden not in serialized
    assert set(RemoteExecutionRequest.model_fields) == {
        "invocation_id", "run_id", "work_order_id", "attempt_id", "objective", "context",
    }
    assert not {
        "workspace_grant", "sandbox", "granted_capabilities", "token", "credential",
    } & set(AgentContextBundle.model_fields)


def test_governed_a2a_cancel_re_resolves_registry_and_unknown_invocation_fails(fixture: DirectFixture) -> None:
    with pytest.raises(ValueError, match="missing"):
        asyncio.run(fixture.adapter.cancel("inv_gov_unknown"))
    result = _invoke(fixture, "inv_gov_cancel")
    assert result.status is InvocationStatus.SUCCEEDED and result.external_invocation_id is not None
    task_id = result.external_invocation_id
    asyncio.run(fixture.adapter.cancel("inv_gov_cancel"))
    assert fixture.client.cancelled == [(task_id, TENANT)]
    fixture.registry.set_runtime_enabled("runtime_a2a", False)
    with pytest.raises(ValueError, match="disabled, unleased, or mismatched"):
        asyncio.run(fixture.adapter.cancel("inv_gov_cancel"))


def test_governed_a2a_rejects_remote_capability_results(fixture: DirectFixture) -> None:
    fixture.client.capability_results = [{"request_id": "cap_x", "status": "ok", "output": "ran"}]
    result = _invoke(fixture, "inv_gov_capability")
    assert result.status is InvocationStatus.FAILED
    assert result.reason_code == "A2A_EXECUTOR_RESULT_INVALID"
    assert len(fixture.client.payloads) == 1


def test_selector_requires_supervision_only_for_process_runtimes(fixture: DirectFixture) -> None:
    registry = fixture.registry
    registry.register_profile(AgentProfile(
        agent_id=AgentId("agent_proc"), display_name="Process executor",
        roles=("executor",), trust_zone=AgentTrustZone.LOCAL_PRIVATE,
    ))
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_proc"), agent_id=AgentId("agent_proc"),
        adapter_kind=AgentAdapterKind.PROCESS, runtime_name="process executor",
        endpoint_ref=MANAGED_ENDPOINT,
    ))
    registry.acquire_runtime("runtime_proc", owner_id="gov-selector", lease_seconds=3600)
    # A dedicated A2A executor avoids the fixture agent, whose single
    # parallel-delegation slot is already consumed by the seeded delegation.
    registry.register_profile(AgentProfile(
        agent_id=AgentId("agent_a2a_sel"), display_name="Governed selector probe",
        roles=("executor",), trust_zone=AgentTrustZone.REMOTE_PRIVATE,
    ))
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_a2a_sel"), agent_id=AgentId("agent_a2a_sel"),
        adapter_kind=AgentAdapterKind.A2A, runtime_name="governed A2A selector probe",
        endpoint_ref=LOOPBACK_ENDPOINT, protocols=("1.0",),
        metadata={"a2a_tenant": TENANT},
    ))
    registry.acquire_runtime("runtime_a2a_sel", owner_id="gov-selector", lease_seconds=3600)
    selector = AgentSelector(fixture.sessions, supervised_adapter_kinds=frozenset({AgentAdapterKind.PROCESS}))
    picked = selector.select(
        required_roles=("executor",),
        allowed_adapter_kinds=frozenset({AgentAdapterKind.PROCESS, AgentAdapterKind.A2A}),
    )
    assert picked is not None and str(picked.runtime_id) == "runtime_a2a_sel"
    assert selector.select(
        required_roles=("executor",),
        allowed_adapter_kinds=frozenset({AgentAdapterKind.PROCESS}),
    ) is None
    launch_profiles = RuntimeLaunchProfileService(fixture.sessions, registry)
    launch_profiles.register_process("runtime_proc", ProcessLaunchSpec(argv=("/usr/bin/true",), cwd="/tmp"))
    now = datetime.now(UTC)
    with fixture.sessions.begin() as session:
        session.add(RuntimeSessionRecord(
            runtime_session_id="rs_proc", runtime_id="runtime_proc",
            launch_mode="PROCESS", supervisor_state="HEALTHY",
            launch_spec_json={"argv": ["/usr/bin/true"], "cwd": "/tmp"},
            launch_profile_sha256=launch_profiles.get("runtime_proc").spec_sha256,
            reattach_state="NOT_APPLICABLE", started_at=now, last_health_at=now,
            version=1, created_at=now, updated_at=now,
        ))
    picked = selector.select(
        required_roles=("executor",),
        allowed_adapter_kinds=frozenset({AgentAdapterKind.PROCESS}),
    )
    assert picked is not None and str(picked.runtime_id) == "runtime_proc"


class _ManagedRoutingClient:
    """HttpAgentClient double for the managed planner/reviewer turns."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

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
        raise AssertionError(f"unexpected managed purpose {purpose}")


class GovFixture:
    """Mixed PROCESS+A2A fixture: managed planner/reviewer, governed A2A executors."""

    def __init__(self, tmp_path: Path) -> None:
        database = tmp_path / "gov.db"
        migrate(database)
        self.sessions: sessionmaker[Session] = session_factory(create_sqlite_engine(database))
        registry = AgentRegistryService(self.sessions)
        registry.register_profile(AgentProfile(
            agent_id=AgentId("agent_planner"), display_name="Planner",
            roles=("planner",), trust_zone=AgentTrustZone.LOCAL_PRIVATE,
        ))
        registry.register_profile(AgentProfile(
            agent_id=AgentId("agent_reviewer"), display_name="Reviewer",
            roles=("reviewer",), trust_zone=AgentTrustZone.LOCAL_PRIVATE,
        ))
        registry.register_profile(AgentProfile(
            agent_id=AgentId("agent_a2a_exec"), display_name="Governed remote executor",
            roles=("executor",), trust_zone=AgentTrustZone.REMOTE_PRIVATE,
        ))
        registry.register_profile(AgentProfile(
            agent_id=AgentId("agent_a2a_multi"), display_name="Governed multi-role remote",
            roles=("planner", "reviewer", "executor"), trust_zone=AgentTrustZone.REMOTE_PRIVATE,
        ))
        for agent_id in ("agent_planner", "agent_reviewer"):
            registry.register_runtime(AgentRuntime(
                runtime_id=AgentRuntimeId(f"runtime_{agent_id}"), agent_id=AgentId(agent_id),
                adapter_kind=AgentAdapterKind.PROCESS, runtime_name=agent_id,
                endpoint_ref=MANAGED_ENDPOINT,
            ))
            registry.acquire_runtime(f"runtime_{agent_id}", owner_id="gov-fixture", lease_seconds=3600)
        for agent_id in ("agent_a2a_exec", "agent_a2a_multi"):
            registry.register_runtime(AgentRuntime(
                runtime_id=AgentRuntimeId(f"runtime_{agent_id}"), agent_id=AgentId(agent_id),
                adapter_kind=AgentAdapterKind.A2A, runtime_name=agent_id,
                endpoint_ref=LOOPBACK_ENDPOINT, protocols=("1.0",),
                metadata={"a2a_tenant": TENANT},
            ))
            registry.acquire_runtime(f"runtime_{agent_id}", owner_id="gov-fixture", lease_seconds=3600)
        self.launch_profiles = RuntimeLaunchProfileService(self.sessions, registry)
        for agent_id in ("agent_planner", "agent_reviewer"):
            self.launch_profiles.register_process(
                f"runtime_{agent_id}", ProcessLaunchSpec(argv=("/usr/bin/true",), cwd="/tmp"),
            )
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            session.add(WorkspaceRecord(workspace_id="ws_gov", name="gov", version=1, created_at=now, updated_at=now))
            session.flush()
            for agent_id in ("agent_planner", "agent_reviewer"):
                session.add(RuntimeSessionRecord(
                    runtime_session_id=f"rs_{agent_id}", runtime_id=f"runtime_{agent_id}",
                    launch_mode="PROCESS", supervisor_state="HEALTHY",
                    launch_spec_json={"argv": ["/usr/bin/true"], "cwd": "/tmp"},
                    launch_profile_sha256=self.launch_profiles.get(f"runtime_{agent_id}").spec_sha256,
                    reattach_state="NOT_APPLICABLE", started_at=now, last_health_at=now,
                    version=1, created_at=now, updated_at=now,
                ))
        self.v2_client = RecordingA2AClient()
        self.managed_client = _ManagedRoutingClient()
        self.broker = CapabilityBroker(
            FakeSandboxBackend(),
            ArtifactService(ContentAddressedArtifactStore(tmp_path / "artifacts"), self.sessions),
            self.sessions,
            command_limits=CommandLimits(
                wall_seconds=10, cpu_seconds=8, memory_mb=768,
                file_size_mb=16, output_bytes=128_000,
            ),
        )
        catalog = AgentAdapterCatalog(self.sessions)
        catalog.register(AgentAdapterKind.PROCESS, ManagedProcessAgentAdapter(
            self.sessions, self.launch_profiles, self.managed_client, self.broker,
            AgentActionBroker(self.sessions),
        ))
        catalog.register(AgentAdapterKind.A2A, GovernedA2ARemoteAgentAdapter(
            self.sessions, client_factory=self.v2_client.bind_endpoint,
        ))
        self.selector = AgentSelector(
            self.sessions,
            allowed_adapter_kinds=frozenset({AgentAdapterKind.PROCESS, AgentAdapterKind.A2A}),
            supervised_adapter_kinds=frozenset({AgentAdapterKind.PROCESS}),
        )
        self.gateway = CollaborationGateway(
            None, None,
            delegations=DelegationService(self.sessions),
            invocations=InvocationService(self.sessions),
            selector=self.selector,
            catalog=catalog,
            context_builder=AgentContextBuilder(ContextBuilder(
                self.sessions,
                ContentAddressedArtifactStore(tmp_path / "artifacts"),
                DeterministicRedactor(),
            )),
            allowed_adapter_kinds_by_purpose={
                DelegationPurpose.PLAN: frozenset({AgentAdapterKind.PROCESS}),
                DelegationPurpose.EXECUTE: frozenset({AgentAdapterKind.PROCESS, AgentAdapterKind.A2A}),
                DelegationPurpose.REVIEW: frozenset({AgentAdapterKind.PROCESS}),
                DelegationPurpose.EVIDENCE: frozenset({AgentAdapterKind.PROCESS}),
                DelegationPurpose.SPECIALIST: frozenset({AgentAdapterKind.PROCESS}),
            },
        )
        self.orchestrator = ResearchOrchestrator(
            self.sessions, collaboration=self.gateway,
            policy=RecordingPolicyEngine(DeterministicPolicyEngine(), self.sessions),
            verifier=FakeVerifier(self.sessions),
            workspace_capabilities=frozenset({Capability.WORKSPACE_WRITE}),
            user_capabilities=frozenset({Capability.WORKSPACE_WRITE}),
            maximum_budget=BudgetLimits(100, 100, 0, 100, 100),
            limits=OrchestrationLimits(max_iterations=8, max_agent_turns=8),
        )

    def create_run(self) -> str:
        return self.orchestrator.create_run(workspace_id="ws_gov", objective="mixed process and a2a closed loop")

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
                source_workspace_id="ws_gov", source_revision="1",
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
                transport_kind="ARCHIVE", transport_handle={"root": "gov-archive"},
                remote_workspace_handle="gov-archive", state="ACTIVE", created_at=now,
            ))


@pytest.fixture
def gov(tmp_path: Path) -> GovFixture:
    return GovFixture(tmp_path)


def test_purpose_restrictions_route_plan_and_review_to_process_only(gov: GovFixture) -> None:
    planner = gov.selector.select(
        required_roles=("planner",),
        allowed_adapter_kinds=frozenset({AgentAdapterKind.PROCESS}),
    )
    assert planner is not None and str(planner.agent_id) == "agent_planner"
    reviewer = gov.selector.select(
        required_roles=("reviewer",),
        allowed_adapter_kinds=frozenset({AgentAdapterKind.PROCESS}),
    )
    assert reviewer is not None and str(reviewer.agent_id) == "agent_reviewer"
    executor = gov.selector.select(
        required_roles=("executor",),
        allowed_adapter_kinds=frozenset({AgentAdapterKind.PROCESS, AgentAdapterKind.A2A}),
    )
    assert executor is not None and str(executor.agent_id) == "agent_a2a_exec"


def test_mixed_process_a2a_closed_loop_completes(gov: GovFixture) -> None:
    run_id = gov.create_run()
    gov.advance_until_executing(run_id)
    attempt = gov.latest_attempt(run_id)
    gov.seed_grant_for(attempt.attempt_id)
    snapshot = asyncio.run(gov.orchestrator.run(run_id, max_steps=30))
    assert snapshot.state.value == ResearchRunState.COMPLETED.value
    with gov.sessions() as session:
        delegations = {
            row.purpose: row for row in session.scalars(
                select(DelegationRecord).where(DelegationRecord.run_id == run_id),
            ).all()
        }
        assert delegations["PLAN"].assigned_agent_id == "agent_planner"
        assert delegations["EXECUTE"].assigned_agent_id == "agent_a2a_exec"
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
        assert session.scalar(select(AuditEventRecord.event_type).where(
            AuditEventRecord.event_type == "A2A_TASK_DISPATCHED",
        )) == "A2A_TASK_DISPATCHED"
    payload = gov.v2_client.payloads[0]
    assert len(gov.v2_client.payloads) == 1
    assert payload["message"]["parts"][0]["mediaType"] == REMOTE_EXECUTION_REQUEST_MEDIA_TYPE
    assert gov.v2_client.endpoints == [LOOPBACK_ENDPOINT]


def test_a2a_capability_result_rejection_fails_closed(gov: GovFixture) -> None:
    gov.v2_client.capability_results = [{"request_id": "cap_x", "status": "ok", "output": "ran"}]
    run_id = gov.create_run()
    gov.advance_until_executing(run_id)
    attempt = gov.latest_attempt(run_id)
    gov.seed_grant_for(attempt.attempt_id)
    assert asyncio.run(gov.orchestrator.advance(run_id)) is True
    with gov.sessions() as session:
        invocation = session.scalar(select(AgentInvocationRecord).where(
            AgentInvocationRecord.run_id == run_id,
            AgentInvocationRecord.purpose == DelegationPurpose.EXECUTE.value,
        ))
        assert invocation is not None and invocation.status == "FAILED"
        assert invocation.failure_category == "OUTPUT_INVALID"
        order = gov.latest_order(run_id)
        assert order.state == WorkOrderState.EXECUTION_FAILED.value
        run = session.get(ResearchRunRecord, run_id)
        assert run is not None and run.state == ResearchRunState.WAITING_EXTERNAL.value
    assert len(gov.v2_client.payloads) == 1
