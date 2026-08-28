from datetime import UTC, datetime
from typing import Protocol
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from researchd.collaboration.contracts import AgentHealth, AgentInvocationRequest, AgentInvocationResult, AgentRuntime
from researchd.domain.enums import AgentAdapterKind
from researchd.domain.ids import AgentId, AgentRuntimeId
from researchd.storage.models import AgentRecord, AgentRuntimeRecord


class AgentAdapter(Protocol):
    async def health(self, runtime: AgentRuntime) -> AgentHealth: ...
    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult: ...
    async def cancel(self, invocation_id: str) -> None: ...


class AgentAdapterCatalog:
    """Trusted runtime-to-adapter resolver; remote descriptors never register here."""
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions
        self._adapters: dict[AgentAdapterKind, AgentAdapter] = {}

    def register(self, kind: AgentAdapterKind, adapter: AgentAdapter) -> None:
        if kind in self._adapters:
            raise ValueError(f"adapter already registered: {kind.value}")
        self._adapters[kind] = adapter

    def resolve(self, runtime_id: str) -> tuple[AgentRuntime, AgentAdapter]:
        with self.sessions() as session:
            row = session.scalar(select(AgentRuntimeRecord).join(AgentRecord, AgentRecord.agent_id == AgentRuntimeRecord.agent_id).where(AgentRuntimeRecord.runtime_id == runtime_id, AgentRuntimeRecord.enabled.is_(True), AgentRecord.enabled.is_(True)))
            if row is None:
                raise LookupError(runtime_id)
            runtime = AgentRuntime(runtime_id=AgentRuntimeId(row.runtime_id), agent_id=AgentId(row.agent_id), adapter_kind=AgentAdapterKind(row.adapter_kind), runtime_name=row.runtime_name, endpoint_ref=row.endpoint_ref, framework=row.framework, model_provider=row.model_provider, model_name=row.model_name, protocols=tuple(row.protocols_json), metadata=dict(row.metadata_json))
        adapter = self._adapters.get(runtime.adapter_kind)
        if adapter is None:
            raise LookupError(f"no adapter for {runtime.adapter_kind.value}")
        return runtime, adapter

    async def health(self, runtime_id: str, *, now: datetime | None = None) -> AgentHealth:
        runtime, adapter = self.resolve(runtime_id)
        reference = now or datetime.now(UTC)
        with self.sessions() as session:
            row = session.get(AgentRuntimeRecord, runtime_id)
            if row is None or row.lease_expires_at is None or row.lease_expires_at <= reference:
                return AgentHealth(healthy=False, reason="runtime lease expired")
        return await adapter.health(runtime)
