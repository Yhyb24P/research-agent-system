from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from researchd.collaboration.contracts import AgentProfile, AgentRuntime, DiscoveredAgentDescriptor
from researchd.collaboration.registry import AgentRegistryService
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.invocation import InvocationService
from researchd.collaboration.contracts import AgentInvocationRequest, AgentInvocationResult, Delegation
from researchd.domain.enums import AgentAdapterKind, AgentTrustZone, DelegationPurpose, InvocationStatus, ResearchRunState
from researchd.domain.ids import DelegationId, InvocationId
from researchd.storage.models import AgentRecord, AgentRuntimeRecord, WorkspaceRecord, ResearchRunRecord
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
