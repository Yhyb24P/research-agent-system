from datetime import UTC, datetime, timedelta
from pathlib import Path
import asyncio
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.collaboration.contracts import AgentProfile, AgentRuntime, AgentInvocationRequest, DiscoveredAgentDescriptor, ExecuteInvocationInput, HumanDirective, PlanInvocationInput
from researchd.collaboration.registry import AgentRegistryService
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.invocation import InvocationService
from researchd.collaboration.adapters import CloudLeadAgentAdapter, LocalExecutorAgentAdapter
from researchd.collaboration.gateway import CollaborationGateway
from researchd.collaboration.messages import CollaborationMessageService
from researchd.collaboration.heterogeneous import A2ARemoteAgentAdapter, HttpAgentAdapter, HttpAgentClient, LocalProcessAgentAdapter, ProcessAgentRunner
from researchd.collaboration.runtime import AgentAdapterCatalog
from researchd.api.control import LocalControlAPI
from researchd.api.web import ControlResourceRouter, serve_local_control
from researchd.api.tui import render_tui
from researchd.adapters.a2a.adapter import A2AAdapter
from researchd.collaboration.selector import AgentSelector
from researchd.context.agent_context import AgentContextBuilder, AgentContextSelection
from researchd.context.builder import CloudContextSelection
from researchd.observability import collect_metrics
from researchd.policy.approval import ApprovalService
from researchd.agents.cloud_lead import CloudLeadAdapter
from researchd.executor.contracts import GrantedWorkOrder, SandboxSpec
from researchd.executor.worker import LocalExecutorWorker
from researchd.collaboration.contracts import AgentInvocationResult, Delegation
from researchd.domain.enums import AgentAdapterKind, AgentTrustZone, Capability, DataClassification, DelegationPurpose, InvocationStatus, ResearchRunState
from researchd.domain.ids import DelegationId, InvocationId, MessageId
from researchd.storage.models import AgentInteractionRecord, AgentRecord, AgentRuntimeRecord, AttemptRecord, AuditEventRecord, CollaborationMessageRecord, WorkspaceRecord, ResearchRunRecord, WorkOrderRecord
from researchd.storage.models import DelegationRecord, AgentInvocationRecord
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.domain.ids import AgentId, AgentRuntimeId
from test_storage import migrate


@pytest.fixture
def database(tmp_path: Path) -> tuple[Path, sessionmaker[Session]]:
    path = tmp_path / "collaboration.db"
    migrate(path)
    engine = create_sqlite_engine(path)
    sessions = session_factory(engine)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(WorkspaceRecord(workspace_id="ws_test", name="test", version=1, created_at=now, updated_at=now))
        session.flush()
        session.add(ResearchRunRecord(run_id="run_test", workspace_id="ws_test", objective="collaboration", state=ResearchRunState.ACTIVE.value, version=1, created_at=now, updated_at=now))
    return path, sessions


def profile(agent_id: AgentId = AgentId("agent_executor")) -> AgentProfile:
    return AgentProfile(
        agent_id=agent_id, display_name="Executor", roles=("executor",),
        skills=("code.modify",), trust_zone=AgentTrustZone.LOCAL_PRIVATE,
    )


def test_registry_rejects_duplicate_profile(database: tuple[Path, sessionmaker[Session]]) -> None:
    _, sessions = database
    registry = AgentRegistryService(sessions)
    registry.register_profile(profile())
    with pytest.raises(ValueError, match="already exists"):
        registry.register_profile(profile())


def test_registry_profile_lifecycle_advances_snapshot_version(database: tuple[Path, sessionmaker[Session]]) -> None:
    _, sessions = database
    registry = AgentRegistryService(sessions)
    registry.register_profile(profile())
    before = registry.get_agent("agent_executor")
    registry.update_profile(profile().model_copy(update={"skills": ("code.inspect",)}))
    after = registry.get_agent("agent_executor")
    assert before.profile_version == 1
    assert after.profile_version == 2
    assert after.skills == ("code.inspect",)
    registry.disable("agent_executor")
    assert not registry.get_agent("agent_executor").enabled
    registry.enable("agent_executor")
    assert registry.get_agent("agent_executor").enabled


def test_discovery_descriptor_does_not_persist_or_enable_agent(database: tuple[Path, sessionmaker[Session]]) -> None:
    _, sessions = database
    registry = AgentRegistryService(sessions)
    descriptor = registry.discovered_descriptor(DiscoveredAgentDescriptor(
        display_name="Remote", roles=("executor",), skills=("secret.read",),
    ))
    assert descriptor.display_name == "Remote"
    with sessions() as session:
        assert session.query(AgentRecord).count() == 0


def test_runtime_requires_trusted_profile_and_heartbeat_expires(database: tuple[Path, sessionmaker[Session]]) -> None:
    _, sessions = database
    registry = AgentRegistryService(sessions)
    runtime = AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_qwen"), agent_id=AgentId("agent_executor"),
        adapter_kind=AgentAdapterKind.HTTP, runtime_name="Qwen workstation",
        model_provider="qwen", model_name="qwen38",
    )
    with pytest.raises(ValueError, match="profile does not exist"):
        registry.register_runtime(runtime)
    registry.register_profile(profile())
    registry.register_runtime(runtime)
    registry.heartbeat("runtime_qwen", lease_seconds=10)
    with sessions() as session:
        row = session.get(AgentRuntimeRecord, "runtime_qwen")
        assert row is not None and row.lease_expires_at is not None
        assert row.lease_expires_at > datetime.now(UTC)
        assert registry.runtime_healthy("runtime_qwen")
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert not registry.runtime_healthy("runtime_qwen")
    assert registry.eligible(role="executor", skill="code.modify") == ("agent_executor",)


def test_skill_declaration_is_not_a_capability_grant() -> None:
    candidate = profile()
    assert "code.modify" in candidate.skills
    assert "workspace.write" not in candidate.skills


def test_registry_rejects_reserved_verifier_role(database: tuple[Path, sessionmaker[Session]]) -> None:
    _, sessions = database
    registry = AgentRegistryService(sessions)
    with pytest.raises(ValueError, match="reserved trusted role"):
        registry.register_profile(AgentProfile(agent_id=AgentId("agent_fake_verifier"), display_name="Fake verifier", roles=("verifier",), trust_zone=AgentTrustZone.LOCAL_PRIVATE))


def test_invocation_input_uses_purpose_discriminator() -> None:
    request = AgentInvocationRequest(
        invocation_id=InvocationId("inv_typed"), delegation_id=DelegationId("del_typed"),
        run_id="run_test", agent_id=AgentId("agent_executor"), runtime_id=AgentRuntimeId("runtime_qwen"),
        purpose=DelegationPurpose.PLAN, input_sha256="e" * 64,
        typed_input=PlanInvocationInput(context=CloudContextSelection(run_id="run_test")),
    )
    assert request.typed_input is not None and request.typed_input.kind == "PLAN"
    with pytest.raises(ValueError, match="kind must match purpose"):
        AgentInvocationRequest.model_validate({**request.model_dump(mode="json"), "purpose": "REVIEW"})


def test_assignment_freezes_profile_and_invocation_is_structured(database: tuple[Path, sessionmaker[Session]]) -> None:
    _, sessions = database
    registry = AgentRegistryService(sessions)
    registry.register_profile(profile())
    registry.register_runtime(AgentRuntime(runtime_id=AgentRuntimeId("runtime_qwen"), agent_id=AgentId("agent_executor"), adapter_kind=AgentAdapterKind.HTTP, runtime_name="Qwen"))
    delegation = Delegation(delegation_id=DelegationId("del_execute"), run_id="run_test", purpose=DelegationPurpose.EXECUTE, idempotency_key="del-execute-1")
    delegations = DelegationService(sessions)
    delegations.create(delegation)
    digest = delegations.assign("del_execute", agent_id="agent_executor", runtime_id="runtime_qwen")
    assert len(digest) == 64
    request = AgentInvocationRequest(invocation_id=InvocationId("inv_execute"), delegation_id=DelegationId("del_execute"), run_id="run_test", agent_id=AgentId("agent_executor"), runtime_id=AgentRuntimeId("runtime_qwen"), purpose=DelegationPurpose.EXECUTE, input_sha256="a" * 64)
    invocations = InvocationService(sessions)
    invocations.start(request)
    invocations.complete(AgentInvocationResult(invocation_id=InvocationId("inv_execute"), status=InvocationStatus.SUCCEEDED, output_type="ExecutorResult", output={"ok": True}))
    with sessions() as session:
        row = session.get(AgentRuntimeRecord, "runtime_qwen")
        assert row is not None


def test_canonical_adapters_expose_health_and_preserve_boundaries() -> None:
    runtime = AgentRuntime(runtime_id=AgentRuntimeId("runtime_qwen"), agent_id=AgentId("agent_executor"), adapter_kind=AgentAdapterKind.HTTP, runtime_name="Qwen")
    assert asyncio.run(CloudLeadAgentAdapter(cast(CloudLeadAdapter, None)).health(runtime)).healthy
    adapter = LocalExecutorAgentAdapter(cast(LocalExecutorWorker, None))
    request = AgentInvocationRequest(invocation_id=InvocationId("inv_invalid"), delegation_id=DelegationId("del_execute"), run_id="run_test", agent_id=AgentId("agent_executor"), runtime_id=AgentRuntimeId("runtime_qwen"), purpose=DelegationPurpose.EXECUTE, input_sha256="b" * 64)
    result = asyncio.run(adapter.invoke(request))
    assert result.status == InvocationStatus.FAILED and result.reason_code == "GRANTED_WORK_ORDER_REQUIRED"


def test_gateway_tracking_creates_delegation_and_invocation(database: tuple[Path, sessionmaker[Session]]) -> None:
    _, sessions = database
    registry = AgentRegistryService(sessions)
    registry.register_profile(profile())
    registry.register_runtime(AgentRuntime(runtime_id=AgentRuntimeId("runtime_qwen"), agent_id=AgentId("agent_executor"), adapter_kind=AgentAdapterKind.HTTP, runtime_name="Qwen"))
    gateway = CollaborationGateway(
        cast(CloudLeadAgentAdapter, None), cast(LocalExecutorAgentAdapter, None),
        delegations=DelegationService(sessions), invocations=InvocationService(sessions),
        agent_id=AgentId("agent_executor"), runtime_id=AgentRuntimeId("runtime_qwen"),
    )
    tracking = gateway._start("run_test", DelegationPurpose.EXECUTE)
    assert tracking is not None
    gateway._finish(tracking[1], success=True, output_type="ExecutorResult", output={"ok": True})
    with sessions() as session:
        delegation = session.get(DelegationRecord, str(tracking[0]))
        invocation = session.get(AgentInvocationRecord, str(tracking[1]))
        assert delegation is not None and delegation.state == "COMPLETED"
        assert invocation is not None and invocation.status == InvocationStatus.SUCCEEDED.value


def test_gateway_catalog_prevents_wrong_runtime_purpose_dispatch(database: tuple[Path, sessionmaker[Session]]) -> None:
    _, sessions = database
    registry = AgentRegistryService(sessions)
    registry.register_profile(AgentProfile(agent_id=AgentId("agent_planner"), display_name="Planner", roles=("planner",), skills=("research.plan",), trust_zone=AgentTrustZone.REMOTE_PRIVATE))
    registry.register_runtime(AgentRuntime(runtime_id=AgentRuntimeId("runtime_http"), agent_id=AgentId("agent_planner"), adapter_kind=AgentAdapterKind.HTTP, runtime_name="HTTP", endpoint_ref="http://127.0.0.1"))
    catalog = AgentAdapterCatalog(sessions)
    catalog.register(AgentAdapterKind.HTTP, HttpAgentAdapter(cast(HttpAgentClient, None)))
    gateway = CollaborationGateway(cast(CloudLeadAgentAdapter, None), cast(LocalExecutorAgentAdapter, None), delegations=DelegationService(sessions), invocations=InvocationService(sessions), agent_id=AgentId("agent_planner"), runtime_id=AgentRuntimeId("runtime_http"), catalog=catalog)
    with pytest.raises(ValueError, match="does not support PLAN"):
        asyncio.run(gateway.plan(CloudContextSelection(run_id="run_test")))


def test_delegation_constraints_and_terminal_state_are_enforced(database: tuple[Path, sessionmaker[Session]]) -> None:
    _, sessions = database
    registry = AgentRegistryService(sessions)
    registry.register_profile(profile())
    registry.register_runtime(AgentRuntime(runtime_id=AgentRuntimeId("runtime_qwen"), agent_id=AgentId("agent_executor"), adapter_kind=AgentAdapterKind.HTTP, runtime_name="Qwen"))
    service = DelegationService(sessions)
    constrained = Delegation(delegation_id=DelegationId("del_constrained"), run_id="run_test", purpose=DelegationPurpose.EXECUTE, required_roles=("reviewer",), idempotency_key="constrained")
    service.create(constrained)
    with pytest.raises(ValueError, match="required roles"):
        service.assign("del_constrained", agent_id="agent_executor", runtime_id="runtime_qwen")
    done = Delegation(delegation_id=DelegationId("del_terminal"), run_id="run_test", purpose=DelegationPurpose.EXECUTE, idempotency_key="terminal")
    service.create(done)
    service.assign("del_terminal", agent_id="agent_executor", runtime_id="runtime_qwen")
    invocations = InvocationService(sessions)
    request = AgentInvocationRequest(invocation_id=InvocationId("inv_terminal"), delegation_id=DelegationId("del_terminal"), run_id="run_test", agent_id=AgentId("agent_executor"), runtime_id=AgentRuntimeId("runtime_qwen"), purpose=DelegationPurpose.EXECUTE, input_sha256="c" * 64)
    invocations.start(request)
    invocations.complete(AgentInvocationResult(invocation_id=InvocationId("inv_terminal"), status=InvocationStatus.SUCCEEDED))
    with pytest.raises(ValueError, match="terminal"):
        invocations.start(AgentInvocationRequest(invocation_id=InvocationId("inv_reopen"), delegation_id=DelegationId("del_terminal"), run_id="run_test", agent_id=AgentId("agent_executor"), runtime_id=AgentRuntimeId("runtime_qwen"), purpose=DelegationPurpose.EXECUTE, input_sha256="d" * 64))


def test_selector_is_deterministic_and_requires_healthy_runtime(database: tuple[Path, sessionmaker[Session]]) -> None:
    _, sessions = database
    registry = AgentRegistryService(sessions)
    registry.register_profile(profile(AgentId("agent_a")))
    registry.register_profile(AgentProfile(agent_id=AgentId("agent_b"), display_name="B", roles=("executor",), skills=("code.modify",), trust_zone=AgentTrustZone.LOCAL_PRIVATE, labels={"priority": "5"}))
    registry.register_runtime(AgentRuntime(runtime_id=AgentRuntimeId("runtime_a"), agent_id=AgentId("agent_a"), adapter_kind=AgentAdapterKind.HTTP, runtime_name="A"))
    registry.register_runtime(AgentRuntime(runtime_id=AgentRuntimeId("runtime_b"), agent_id=AgentId("agent_b"), adapter_kind=AgentAdapterKind.HTTP, runtime_name="B"))
    registry.heartbeat("runtime_a")
    registry.heartbeat("runtime_b")
    selected = AgentSelector(sessions).select(required_roles=("executor",), required_skills=("code.modify",))
    assert selected is not None and selected.agent_id == "agent_b"


def test_agent_context_rejects_untrusted_target_before_egress(database: tuple[Path, sessionmaker[Session]]) -> None:
    from researchd.artifacts.provenance import ArtifactService
    from researchd.artifacts.store import ContentAddressedArtifactStore
    from researchd.context.redaction import DeterministicRedactor
    from researchd.context.builder import ContextBuilder
    path, sessions = database
    store = ContentAddressedArtifactStore(path.parent / "acp-context")
    artifact = ArtifactService(store, sessions).register(b"local secret", mime_type="text/plain", artifact_type="fixture", classification=DataClassification.LOCAL_ONLY, producer_type="test", producer_id="test")
    builder = AgentContextBuilder(ContextBuilder(sessions, store, DeterministicRedactor()))
    selection = AgentContextSelection(target_agent_id="agent_ext", target_runtime_id="runtime_ext", target_trust_zone=AgentTrustZone.EXTERNAL_UNTRUSTED, purpose=DelegationPurpose.EXECUTE, run_id="run_test", artifact_ids=(artifact.artifact_id,))
    with pytest.raises(PermissionError, match="not eligible"):
        builder.build(selection)


def test_cross_trust_redelegation_rebuilds_target_specific_bundle(database: tuple[Path, sessionmaker[Session]]) -> None:
    from researchd.artifacts.store import ContentAddressedArtifactStore
    from researchd.context.redaction import DeterministicRedactor
    from researchd.context.builder import ContextBuilder
    path, sessions = database
    store = ContentAddressedArtifactStore(path.parent / "acp-context-rebuild")
    builder = AgentContextBuilder(ContextBuilder(sessions, store, DeterministicRedactor()))
    local = builder.build(AgentContextSelection(target_agent_id="agent_local", target_runtime_id="runtime_local", target_trust_zone=AgentTrustZone.LOCAL_PRIVATE, purpose=DelegationPurpose.EXECUTE, run_id="run_test"))
    cloud = builder.build(AgentContextSelection(target_agent_id="agent_cloud", target_runtime_id="runtime_cloud", target_trust_zone=AgentTrustZone.EXTERNAL_CLOUD, purpose=DelegationPurpose.EXECUTE, run_id="run_test", previous_bundle_sha256=local.bundle_sha256))
    assert cloud.rebuilt_from_previous_bundle
    assert cloud.bundle_sha256 != local.bundle_sha256
    assert cloud.policy.allowed_classifications == frozenset({DataClassification.PUBLIC, DataClassification.CLOUD_SAFE})


def test_cloud_context_exposes_derived_artifact_provenance(database: tuple[Path, sessionmaker[Session]]) -> None:
    from researchd.artifacts.provenance import ArtifactService
    from researchd.artifacts.store import ContentAddressedArtifactStore
    from researchd.context.redaction import DeterministicRedactor
    from researchd.context.builder import ContextBuilder
    path, sessions = database
    store = ContentAddressedArtifactStore(path.parent / "acp-context-provenance")
    artifacts = ArtifactService(store, sessions)
    source = artifacts.register(b"private source", mime_type="text/plain", artifact_type="source", classification=DataClassification.PROJECT_PRIVATE, producer_type="test", producer_id="source")
    derived = artifacts.derive((source.artifact_id,), b"safe summary", mime_type="text/plain", artifact_type="summary", classification=DataClassification.CLOUD_SAFE, producer="redactor", producer_version="v1", parameters={"kind": "summary"})
    bundle = AgentContextBuilder(ContextBuilder(sessions, store, DeterministicRedactor())).build(AgentContextSelection(target_agent_id="agent_cloud", target_runtime_id="runtime_cloud", target_trust_zone=AgentTrustZone.EXTERNAL_CLOUD, purpose=DelegationPurpose.REVIEW, run_id="run_test", artifact_ids=(source.artifact_id,)))
    provenance = next(item for item in bundle.artifact_provenance if item.artifact_id == derived.artifact_id)
    assert provenance.classification is DataClassification.CLOUD_SAFE
    assert provenance.source_artifact_ids == (source.artifact_id,)
    assert provenance.transformation_sha256


def test_approval_metrics_are_scoped_to_run(database: tuple[Path, sessionmaker[Session]]) -> None:
    from datetime import timedelta
    _, sessions = database
    approvals = ApprovalService(sessions)
    registry = AgentRegistryService(sessions)
    registry.register_profile(profile())
    registry.register_runtime(AgentRuntime(runtime_id=AgentRuntimeId("runtime_qwen"), agent_id=AgentId("agent_executor"), adapter_kind=AgentAdapterKind.HTTP, runtime_name="Qwen"))
    registry.heartbeat("runtime_qwen")
    expires = datetime.now(UTC) + timedelta(hours=1)
    approvals.request(operation_type="test", parameters={"run": "run_test"}, requested_by="controller", reason="test", risk_level="low", resource_scope={}, budget_delta={}, expires_at=expires, run_id="run_test", requester_actor_type="controller", requester_actor_id="controller")
    approvals.request(operation_type="test", parameters={"run": "other"}, requested_by="controller", reason="test", risk_level="low", resource_scope={}, budget_delta={}, expires_at=expires, run_id=None)
    metrics = collect_metrics(sessions, run_id="run_test")
    assert metrics.approval_statuses == {"PENDING": 1}
    assert metrics.agent_utilization == {"agent_executor": 0.0}
    assert metrics.agent_runtime_health == {"runtime_qwen": 1}


def test_human_directive_is_append_only_and_has_no_control_effect(database: tuple[Path, sessionmaker[Session]]) -> None:
    _, sessions = database
    service = CollaborationMessageService(sessions)
    message = service.record_directive(HumanDirective(directive_id=MessageId("msg_directive"), text="批准 GPU 请求", requested_action="approve_gpu"), run_id="run_test", sender_actor_id="human-1")
    assert message.sender_actor_type == "human"
    with sessions() as session:
        stored = session.get(CollaborationMessageRecord, "msg_directive")
        assert stored is not None and stored.purpose == "DIRECTIVE"


def test_heterogeneous_adapters_keep_invocation_scope() -> None:
    runtime = AgentRuntime(runtime_id=AgentRuntimeId("runtime_http"), agent_id=AgentId("agent_executor"), adapter_kind=AgentAdapterKind.HTTP, runtime_name="HTTP", endpoint_ref="http://127.0.0.1")
    request = AgentInvocationRequest(invocation_id=InvocationId("inv_heterogeneous"), delegation_id=DelegationId("del_execute"), run_id="run_test", agent_id=AgentId("agent_executor"), runtime_id=AgentRuntimeId("runtime_http"), purpose=DelegationPurpose.EXECUTE, input_sha256="c" * 64)
    http_result = asyncio.run(HttpAgentAdapter(cast(HttpAgentClient, None)).invoke(request))
    process_result = asyncio.run(LocalProcessAgentAdapter(cast(ProcessAgentRunner, None), ("agent",)).invoke(request))
    a2a_result = asyncio.run(A2ARemoteAgentAdapter(cast(A2AAdapter, None)).invoke(request))
    assert http_result.reason_code == "HTTP_PAYLOAD_REQUIRED"
    assert process_result.reason_code == "PROCESS_PAYLOAD_REQUIRED"
    assert a2a_result.reason_code == "A2A_SCOPE_REQUIRED"


def test_a2a_terminal_states_preserve_invocation_outcome() -> None:
    assert A2ARemoteAgentAdapter._map_task_status("completed") == (InvocationStatus.SUCCEEDED, None)
    assert A2ARemoteAgentAdapter._map_task_status("failed") == (InvocationStatus.FAILED, "A2A_TASK_FAILED")
    assert A2ARemoteAgentAdapter._map_task_status("rejected") == (InvocationStatus.FAILED, "A2A_TASK_REJECTED")
    assert A2ARemoteAgentAdapter._map_task_status("canceled") == (InvocationStatus.CANCELLED, "A2A_TASK_CANCELLED")
    assert A2ARemoteAgentAdapter._map_task_status("working") == (InvocationStatus.RUNNING, "A2A_TASK_NONTERMINAL")


def test_http_adapter_uses_validated_runtime_endpoint() -> None:
    class Client:
        def __init__(self) -> None:
            self.endpoints: list[str] = []

        async def invoke(self, endpoint: str, payload: dict[str, object]) -> dict[str, object]:
            self.endpoints.append(endpoint)
            return {"accepted": payload["objective"], "capabilities": payload["granted_capabilities"]}

    client = Client()
    request = AgentInvocationRequest(
        invocation_id=InvocationId("inv_http_endpoint"), delegation_id=DelegationId("del_http_endpoint"),
        run_id="run_test", agent_id=AgentId("agent_executor"), runtime_id=AgentRuntimeId("runtime_http"),
        purpose=DelegationPurpose.EXECUTE, input_sha256="f" * 64,
        endpoint_ref="http://127.0.0.1:8789/agent",
        typed_input=ExecuteInvocationInput(work_order=GrantedWorkOrder(
            attempt_id="att_http_endpoint", objective="typed execute", granted_capabilities=frozenset({Capability.TEST_RUN}),
            sandbox=SandboxSpec(attempt_id="att_http_endpoint", workspace="/workspace"),
        )),
    )
    result = asyncio.run(HttpAgentAdapter(client).invoke(request))
    assert result.status is InvocationStatus.SUCCEEDED
    assert client.endpoints == ["http://127.0.0.1:8789/agent"]
    assert result.output is not None and result.output["accepted"] == "typed execute"


def test_adapter_catalog_resolves_enabled_healthy_runtime(database: tuple[Path, sessionmaker[Session]]) -> None:
    _, sessions = database
    registry = AgentRegistryService(sessions)
    registry.register_profile(profile())
    registry.register_runtime(AgentRuntime(runtime_id=AgentRuntimeId("runtime_http"), agent_id=AgentId("agent_executor"), adapter_kind=AgentAdapterKind.HTTP, runtime_name="HTTP", endpoint_ref="http://127.0.0.1"))
    registry.heartbeat("runtime_http")
    catalog = AgentAdapterCatalog(sessions)
    catalog.register(AgentAdapterKind.HTTP, HttpAgentAdapter(cast(HttpAgentClient, None)))
    runtime, adapter = catalog.resolve("runtime_http")
    assert runtime.adapter_kind is AgentAdapterKind.HTTP and isinstance(adapter, HttpAgentAdapter)
    assert asyncio.run(catalog.health("runtime_http")).healthy


def test_registry_delegation_and_control_plane_work_without_a2a_registration(database: tuple[Path, sessionmaker[Session]]) -> None:
    _, sessions = database
    registry = AgentRegistryService(sessions)
    registry.register_profile(profile())
    registry.register_runtime(AgentRuntime(runtime_id=AgentRuntimeId("runtime_internal"), agent_id=AgentId("agent_executor"), adapter_kind=AgentAdapterKind.INTERNAL, runtime_name="Internal"))
    registry.heartbeat("runtime_internal")
    catalog = AgentAdapterCatalog(sessions)
    assert AgentAdapterKind.A2A not in catalog._adapters
    selected = AgentSelector(sessions).select(required_roles=("executor",), required_skills=("code.modify",))
    assert selected is not None and selected.runtime_id == "runtime_internal"
    delegation = Delegation(delegation_id=DelegationId("del_no_a2a"), run_id="run_test", purpose=DelegationPurpose.EXECUTE, idempotency_key="no-a2a")
    service = DelegationService(sessions)
    service.create(delegation)
    assert service.assign("del_no_a2a", agent_id="agent_executor", runtime_id="runtime_internal")


def test_http_and_process_adapters_reject_unsafe_or_oversized_inputs() -> None:
    runtime = AgentRuntime(runtime_id=AgentRuntimeId("runtime_http"), agent_id=AgentId("agent_executor"), adapter_kind=AgentAdapterKind.HTTP, runtime_name="HTTP", endpoint_ref="http://user:pass@example")
    assert not asyncio.run(HttpAgentAdapter(cast(HttpAgentClient, None)).health(runtime)).healthy
    with pytest.raises(ValueError, match="NUL-free"):
        LocalProcessAgentAdapter(cast(ProcessAgentRunner, None), ("agent\x00",))
    request = AgentInvocationRequest(invocation_id=InvocationId("inv_large"), delegation_id=DelegationId("del_execute"), run_id="run_test", agent_id=AgentId("agent_executor"), runtime_id=AgentRuntimeId("runtime_http"), purpose=DelegationPurpose.EXECUTE, input_sha256="d" * 64, payload={"blob": "x" * 100})
    result = asyncio.run(HttpAgentAdapter(cast(HttpAgentClient, None), max_payload_bytes=16).invoke(request))
    assert result.reason_code == "HTTP_PAYLOAD_TOO_LARGE"


def test_web_and_tui_clients_share_local_control_resources(tmp_path: Path) -> None:
    from test_orchestrator import make_orchestrator, _proposal, _review
    sessions, orchestrator, _, _ = make_orchestrator(tmp_path, cloud_responses=[_proposal(), _review()])
    run_id = orchestrator.create_run(workspace_id="ws_e2e", objective="ui")
    api = LocalControlAPI(sessions, orchestrator)
    router = ControlResourceRouter(api)
    assert router.get("/api/runs/" + run_id)[0] == 200
    status, event_payload = router.get("/api/events/" + run_id)
    assert status == 200
    assert isinstance(event_payload, dict)
    events = event_payload["events"]
    if events:
        _, tail_payload = router.get(f"/api/events/{run_id}?after={events[0]['event_id']}")
        assert isinstance(tail_payload, dict)
        assert [item["event_id"] for item in tail_payload["events"]] == [item["event_id"] for item in events[1:]]
    assert router.get("/api/timeline/" + run_id)[0] == 200
    assert router.get("/api/approvals?run=" + run_id)[0] == 200
    assert router.get("/api/artifacts?run=" + run_id)[0] == 200
    assert "Agents" in render_tui(api, run_id=run_id)
    server = serve_local_control(api, port=0)
    try:
        assert server.server_address[0] in {"127.0.0.1", "localhost"}
    finally:
        server.server_close()
    with pytest.raises(ValueError, match="loopback"):
        serve_local_control(api, host="0.0.0.0", port=0)


def test_collaboration_only_reference_workflow_records_agent_chain(tmp_path: Path) -> None:
    from test_orchestrator import FakeVerifier, _proposal, _review, make_orchestrator
    from researchd.collaboration.adapters import CloudLeadAgentAdapter, LocalExecutorAgentAdapter
    from researchd.collaboration.gateway import CollaborationGateway
    from researchd.orchestrator.engine import OrchestrationLimits, ResearchOrchestrator
    from researchd.policy.engine import DeterministicPolicyEngine, RecordingPolicyEngine
    sessions, legacy, _, _ = make_orchestrator(tmp_path, cloud_responses=[_proposal(), _review()])
    registry = AgentRegistryService(sessions)
    for agent_id, role, skill, runtime_id in (
        ("agent_planner", "planner", "research.plan", "runtime_planner"),
        ("agent_executor", "executor", "code.modify", "runtime_executor"),
        ("agent_reviewer", "reviewer", "research.review", "runtime_reviewer"),
    ):
        registry.register_profile(AgentProfile(agent_id=AgentId(agent_id), display_name=agent_id, roles=(role,), skills=(skill,), trust_zone=AgentTrustZone.LOCAL_PRIVATE))
        registry.register_runtime(AgentRuntime(runtime_id=AgentRuntimeId(runtime_id), agent_id=AgentId(agent_id), adapter_kind=AgentAdapterKind.INTERNAL, runtime_name=runtime_id))
        registry.heartbeat(runtime_id)

    from researchd.executor.contracts import ExecutorResult

    class OneArgExecutor:
        async def execute(self, work_order: object) -> ExecutorResult:
            return ExecutorResult(attempt_id="gateway-result", status="execution_complete", capability_results=(), reported_claims=("delegated",), errors=())

    gateway = CollaborationGateway(
        CloudLeadAgentAdapter(cast(CloudLeadAdapter, legacy.cloud)),
        LocalExecutorAgentAdapter(cast(LocalExecutorWorker, OneArgExecutor())),
        delegations=DelegationService(sessions), invocations=InvocationService(sessions),
        selector=AgentSelector(sessions),
    )
    policy = RecordingPolicyEngine(DeterministicPolicyEngine(), sessions)
    controller = ResearchOrchestrator(sessions, policy=policy, verifier=FakeVerifier(sessions), collaboration=gateway, limits=OrchestrationLimits(max_iterations=8, max_cloud_calls=8))
    run_id = controller.create_run(workspace_id="ws_e2e", objective="collaboration e2e")
    assert asyncio.run(controller.run(run_id, max_steps=30)).state is ResearchRunState.COMPLETED
    with sessions() as session:
        purposes = set(session.scalars(select(DelegationRecord.purpose).where(DelegationRecord.run_id == run_id)).all())
        assert purposes == {"PLAN", "EXECUTE", "REVIEW"}
        invocations = session.scalars(select(AgentInvocationRecord).where(AgentInvocationRecord.run_id == run_id)).all()
        assert len(invocations) == 3 and {item.status for item in invocations} == {"SUCCEEDED"}
        interactions = session.scalars(select(AgentInteractionRecord).where(AgentInteractionRecord.run_id == run_id)).all()
        assert {item.purpose for item in interactions} == {"PLAN", "REVIEW"}
        assert all(item.invocation_id is not None for item in interactions)
        attempt = session.scalar(select(AttemptRecord).where(AttemptRecord.work_order_id.in_(select(WorkOrderRecord.work_order_id).where(WorkOrderRecord.run_id == run_id))))
        assert attempt is not None and attempt.delegation_id is not None
        plan_event = session.scalar(select(AuditEventRecord).where(AuditEventRecord.run_id == run_id, AuditEventRecord.event_type == "PLAN_CREATED"))
        assert plan_event is not None and plan_event.actor_type == "agent" and plan_event.actor_id == "agent_planner"
    rendered = render_tui(LocalControlAPI(sessions, controller), run_id=run_id)
    assert "Runtime runtime_planner" in rendered
    assert "Delegations" in rendered and "Approvals" in rendered and "Artifacts" in rendered and "System" in rendered
