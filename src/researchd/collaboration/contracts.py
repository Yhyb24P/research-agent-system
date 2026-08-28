from pydantic import Field, PositiveInt

from researchd.domain.base import DomainModel
from researchd.domain.enums import AgentAdapterKind, AgentTrustZone
from researchd.domain.ids import AgentId, AgentRuntimeId


class AgentProfile(DomainModel):
    agent_id: AgentId
    display_name: str = Field(min_length=1)
    roles: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    trust_zone: AgentTrustZone
    constraints: tuple[str, ...] = ()
    labels: dict[str, str] = Field(default_factory=dict)
    max_parallel_delegations: PositiveInt = 1
    enabled: bool = True
    profile_version: PositiveInt = 1


class AgentRuntime(DomainModel):
    runtime_id: AgentRuntimeId
    agent_id: AgentId
    adapter_kind: AgentAdapterKind
    runtime_name: str = Field(min_length=1)
    endpoint_ref: str | None = None
    framework: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    protocols: tuple[str, ...] = ()
    metadata: dict[str, str] = Field(default_factory=dict)


class DiscoveredAgentDescriptor(DomainModel):
    """Untrusted discovery result; it cannot grant profile or capabilities."""

    display_name: str = Field(min_length=1)
    roles: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    endpoint_ref: str | None = None
