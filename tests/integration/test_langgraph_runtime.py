import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.agents.research_critic import build_research_critic_graph
from researchd.collaboration.contracts import (
    AgentProfile,
    AgentRuntime,
    SpecialistClaim,
    SpecialistInvocationInput,
)
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.gateway import CollaborationGateway
from researchd.collaboration.invocation import InvocationService
from researchd.collaboration.langgraph_runtime import LangGraphAgentAdapter, LangGraphExecutable
from researchd.collaboration.registry import AgentRegistryService
from researchd.collaboration.runtime import AgentAdapterCatalog
from researchd.collaboration.selector import AgentSelector
from researchd.domain.enums import AgentAdapterKind, AgentTrustZone, InvocationStatus
from researchd.domain.ids import AgentId, AgentRuntimeId
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import AgentInvocationRecord, DelegationRecord, ResearchRunRecord, WorkspaceRecord


pytest.importorskip("langgraph", reason="install the langgraph-agent extra")


ROOT = Path(__file__).parents[2]


def _database(path: Path) -> sessionmaker[Session]:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")
    sessions = session_factory(create_sqlite_engine(path))
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(WorkspaceRecord(
            workspace_id="ws_specialist", name="specialist", version=1,
            created_at=now, updated_at=now,
        ))
        session.flush()
        session.add(ResearchRunRecord(
            run_id="run_specialist", workspace_id="ws_specialist",
            objective="critique evidence coverage", state="ACTIVE",
            max_iterations=8, max_agent_turns=24, iterations_used=0,
            agent_turns_used=0, cancellation_requested=False,
            version=1, created_at=now, updated_at=now,
        ))
    return sessions


class _RecordingGraph:
    def __init__(self, delegate: LangGraphExecutable) -> None:
        self.delegate = delegate
        self.configs: list[dict[str, Any]] = []

    async def ainvoke(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.configs.append(config or {})
        return await self.delegate.ainvoke(input, config)


def test_real_langgraph_specialist_runs_through_canonical_agent_plane(tmp_path: Path) -> None:
    sessions = _database(tmp_path / "langgraph.db")
    registry = AgentRegistryService(sessions)
    registry.register_profile(AgentProfile(
        agent_id=AgentId("agent_research_critic"),
        display_name="Research Critic",
        roles=("specialist",),
        skills=("research.critique",),
        trust_zone=AgentTrustZone.LOCAL_PRIVATE,
    ))
    runtime = AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_research_critic"),
        agent_id=AgentId("agent_research_critic"),
        adapter_kind=AgentAdapterKind.LANGGRAPH,
        runtime_name="Research Critic LangGraph",
        framework="langgraph",
        protocols=("langgraph:1.2",),
    )
    registry.register_runtime(runtime)
    registry.acquire_runtime("runtime_research_critic", owner_id="langgraph-test")

    graph = _RecordingGraph(build_research_critic_graph())
    adapter = LangGraphAgentAdapter()
    adapter.register("runtime_research_critic", graph)
    catalog = AgentAdapterCatalog(sessions)
    catalog.register(AgentAdapterKind.LANGGRAPH, adapter)
    assert asyncio.run(catalog.health("runtime_research_critic")).healthy

    gateway = CollaborationGateway(
        delegations=DelegationService(sessions),
        invocations=InvocationService(sessions),
        selector=AgentSelector(sessions),
        catalog=catalog,
    )
    result = asyncio.run(gateway.specialist(
        "run_specialist",
        SpecialistInvocationInput(
            objective="identify claims without traceable evidence",
            claims=(
                SpecialistClaim(
                    claim_id="claim_supported",
                    statement="The primary source supports the conclusion.",
                    evidence_refs=("artifact_primary",),
                ),
                SpecialistClaim(
                    claim_id="claim_gap",
                    statement="The generalization applies to every deployment.",
                ),
            ),
            review_focus=("evidence coverage", "overgeneralization"),
        ),
    ))
    assert result.recommendation == "REVISE"
    assert result.cited_evidence_refs == ("artifact_primary",)
    assert [(item.code, item.claim_id) for item in result.findings] == [
        ("CLAIM_WITHOUT_EVIDENCE", "claim_gap")
    ]

    with sessions() as session:
        delegation = session.scalar(select(DelegationRecord).where(
            DelegationRecord.run_id == "run_specialist",
            DelegationRecord.purpose == "SPECIALIST",
        ))
        invocation = session.scalar(select(AgentInvocationRecord).where(
            AgentInvocationRecord.run_id == "run_specialist",
            AgentInvocationRecord.purpose == "SPECIALIST",
        ))
        assert delegation is not None and delegation.assigned_agent_id == "agent_research_critic"
        assert delegation.state == "COMPLETED"
        assert invocation is not None and invocation.status == InvocationStatus.SUCCEEDED.value
        assert invocation.output_type == "ResearchCriticResult"
        assert invocation.output_json == result.model_dump(mode="json")
    assert graph.configs == [{
        "configurable": {"thread_id": delegation.delegation_id},
        "metadata": {
            "invocation_id": invocation.invocation_id,
            "agent_id": "agent_research_critic",
            "runtime_id": "runtime_research_critic",
        },
    }]
