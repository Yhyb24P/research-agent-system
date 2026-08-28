from datetime import UTC, datetime, timedelta
from pathlib import Path
import asyncio
from typing import cast

import pytest
from sqlalchemy.orm import Session, sessionmaker

from researchd.collaboration.contracts import AgentProfile, AgentRuntime, DiscoveredAgentDescriptor
from researchd.collaboration.registry import AgentRegistryService
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.invocation import InvocationService
from researchd.collaboration.adapters import CloudLeadAgentAdapter, LocalExecutorAgentAdapter
from researchd.collaboration.gateway import CollaborationGateway
from researchd.collaboration.selector import AgentSelector
from researchd.agents.cloud_lead import CloudLeadAdapter
from researchd.executor.worker import LocalExecutorWorker
from researchd.collaboration.contracts import AgentInvocationRequest, AgentInvocationResult, Delegation
from researchd.domain.enums import AgentAdapterKind, AgentTrustZone, DelegationPurpose, InvocationStatus, ResearchRunState
from researchd.domain.ids import DelegationId, InvocationId
from researchd.storage.models import AgentRecord, AgentRuntimeRecord, WorkspaceRecord, ResearchRunRecord
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
        assert delegation is not None and delegation.state == "ASSIGNED"
        assert invocation is not None and invocation.status == InvocationStatus.SUCCEEDED.value


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
