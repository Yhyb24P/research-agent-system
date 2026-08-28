from typing import Annotated, Literal
from pydantic import Field, PositiveInt

from researchd.domain.base import DomainModel
from researchd.domain.enums import AgentAdapterKind, AgentTrustZone, DelegationPurpose, DelegationState, InvocationStatus
from researchd.domain.ids import AgentId, AgentRuntimeId, DelegationId, InvocationId, MessageId
from researchd.context.builder import CloudContextSelection
from researchd.executor.contracts import GrantedWorkOrder


class PlanInvocationInput(DomainModel):
    kind: Literal["PLAN"] = "PLAN"
    context: CloudContextSelection


class ReviewInvocationInput(DomainModel):
    kind: Literal["REVIEW"] = "REVIEW"
    context: CloudContextSelection


class ExecuteInvocationInput(DomainModel):
    kind: Literal["EXECUTE"] = "EXECUTE"
    work_order: GrantedWorkOrder


class EvidenceInvocationInput(DomainModel):
    kind: Literal["EVIDENCE"] = "EVIDENCE"
    context: CloudContextSelection


InvocationInput = Annotated[
    PlanInvocationInput | ReviewInvocationInput | ExecuteInvocationInput | EvidenceInvocationInput,
    Field(discriminator="kind"),
]


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


class Delegation(DomainModel):
    delegation_id: DelegationId
    run_id: str
    work_order_id: str | None = None
    purpose: DelegationPurpose
    required_roles: tuple[str, ...] = ()
    required_skills: tuple[str, ...] = ()
    required_trust_zones: tuple[AgentTrustZone, ...] = ()
    assigned_agent_id: AgentId | None = None
    assigned_runtime_id: AgentRuntimeId | None = None
    agent_profile_version: int | None = None
    agent_snapshot: dict[str, object] | None = None
    assignment_sha256: str | None = None
    state: DelegationState = DelegationState.PENDING
    idempotency_key: str


class AgentInvocationRequest(DomainModel):
    invocation_id: InvocationId
    delegation_id: DelegationId
    run_id: str
    work_order_id: str | None = None
    attempt_id: str | None = None
    agent_id: AgentId
    runtime_id: AgentRuntimeId
    purpose: DelegationPurpose
    input_sha256: str
    typed_input: InvocationInput | None = None
    # Deprecated compatibility escape hatch for pre-ACP adapters. New gateway
    # calls must use typed_input so purpose and payload cannot drift apart.
    payload: object | None = None


class AgentInvocationResult(DomainModel):
    invocation_id: InvocationId
    status: InvocationStatus
    output_type: str | None = None
    output: dict[str, object] | None = None
    reason_code: str | None = None


class HumanDirective(DomainModel):
    directive_id: MessageId
    text: str = Field(min_length=1, max_length=16_384)
    requested_action: str | None = None


class CollaborationMessage(DomainModel):
    message_id: MessageId
    run_id: str
    sender_actor_type: str = Field(min_length=1)
    sender_actor_id: str = Field(min_length=1)
    recipient_agent_id: AgentId | None = None
    purpose: str = Field(min_length=1)
    body: str = Field(min_length=1, max_length=32_768)
    metadata: dict[str, str] = Field(default_factory=dict)


class AgentHealth(DomainModel):
    healthy: bool
    reason: str | None = None
