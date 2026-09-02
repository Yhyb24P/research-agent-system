from datetime import datetime
from typing import Annotated, Literal
from pydantic import Field, PositiveInt, model_validator

from researchd.domain.base import DomainModel
from researchd.domain.enums import AgentAdapterKind, AgentFailureCategory, AgentTrustZone, CollaborationPurpose, DataClassification, DelegationPurpose, DelegationState, InvocationStatus
from researchd.domain.ids import AgentId, AgentRuntimeId, DelegationId, InvocationId, MessageId
from researchd.context.builder import CloudContextSelection
from researchd.context.agent_context import AgentContextBundle
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


class SpecialistClaim(DomainModel):
    claim_id: str = Field(min_length=1, max_length=128)
    statement: str = Field(min_length=1, max_length=16_384)
    evidence_refs: tuple[str, ...] = ()


class SpecialistInvocationInput(DomainModel):
    kind: Literal["SPECIALIST"] = "SPECIALIST"
    objective: str = Field(min_length=1, max_length=16_384)
    claims: tuple[SpecialistClaim, ...] = ()
    review_focus: tuple[str, ...] = ()


class SpecialistFinding(DomainModel):
    code: str = Field(min_length=1, max_length=128)
    severity: Literal["INFO", "WARNING", "ERROR"]
    detail: str = Field(min_length=1, max_length=16_384)
    claim_id: str | None = None


class ResearchCriticResult(DomainModel):
    summary: str = Field(min_length=1, max_length=16_384)
    findings: tuple[SpecialistFinding, ...] = ()
    recommendation: Literal["ACCEPT", "REVISE"]
    cited_evidence_refs: tuple[str, ...] = ()


InvocationInput = Annotated[
    PlanInvocationInput | ReviewInvocationInput | ExecuteInvocationInput | EvidenceInvocationInput | SpecialistInvocationInput,
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


class AgentRuntimeLease(DomainModel):
    lease_id: str = Field(min_length=1, max_length=128)
    runtime_id: AgentRuntimeId
    owner_id: str = Field(min_length=1, max_length=128)
    acquired_at: datetime
    expires_at: datetime


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
    workspace_grant_id: str | None = None
    agent_id: AgentId
    runtime_id: AgentRuntimeId
    purpose: DelegationPurpose
    input_sha256: str
    endpoint_ref: str | None = None
    context_bundle: AgentContextBundle | None = None
    typed_input: InvocationInput

    @model_validator(mode="after")
    def typed_input_matches_purpose(self) -> "AgentInvocationRequest":
        if self.typed_input.kind != self.purpose.value:
            raise ValueError("typed invocation input kind must match purpose")
        return self


class AgentInvocationResult(DomainModel):
    invocation_id: InvocationId
    status: InvocationStatus
    external_invocation_id: str | None = Field(default=None, max_length=256)
    output_type: str | None = None
    output: dict[str, object] | None = None
    failure_category: AgentFailureCategory | None = None
    reason_code: str | None = Field(default=None, max_length=128)

    @model_validator(mode="before")
    @classmethod
    def normalize_failure_category(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        status = value.get("status")
        if isinstance(status, InvocationStatus):
            status = status.value
        if status == InvocationStatus.SUCCEEDED.value:
            return value
        reason = value.get("reason_code")
        if not isinstance(reason, str) or not reason:
            raise ValueError("terminal non-success invocation requires a bounded reason")
        if value.get("failure_category") is None:
            value = {**value, "failure_category": classify_agent_failure(reason)}
        return value


def classify_agent_failure(reason: str) -> AgentFailureCategory:
    normalized = reason.upper()
    if "CANCEL" in normalized:
        return AgentFailureCategory.CANCELLED
    if "RECONCILIATION" in normalized:
        return AgentFailureCategory.RECONCILIATION_REQUIRED
    if "TIMEOUT" in normalized or "TIMED_OUT" in normalized:
        return AgentFailureCategory.INVOCATION_TIMEOUT
    if "UNAVAILABLE" in normalized or "NOT_REGISTERED" in normalized:
        return AgentFailureCategory.RUNTIME_UNAVAILABLE
    if any(token in normalized for token in ("HTTPSTATUS", "CONNECT", "TRANSPORT")):
        return AgentFailureCategory.TRANSPORT_FAILED
    if any(token in normalized for token in ("INVALID", "MALFORMED", "TOO_LARGE", "VALUEERROR")):
        return AgentFailureCategory.OUTPUT_INVALID
    return AgentFailureCategory.AGENT_REPORTED_FAILURE


class AgentInvocationFailure(RuntimeError):
    """Typed control-plane signal for an already-attributed Agent failure."""

    def __init__(self, result: AgentInvocationResult) -> None:
        if result.status is InvocationStatus.SUCCEEDED or result.failure_category is None:
            raise ValueError("AgentInvocationFailure requires a failed invocation result")
        self.result = result
        self.failure_category = result.failure_category
        super().__init__(result.reason_code)


class HumanDirective(DomainModel):
    directive_id: MessageId
    text: str = Field(min_length=1, max_length=16_384)
    requested_action: str | None = None


class CollaborationMessage(DomainModel):
    message_id: MessageId
    run_id: str
    work_order_id: str | None = None
    delegation_id: DelegationId | None = None
    invocation_id: InvocationId | None = None
    reply_to_message_id: MessageId | None = None
    sender_actor_type: str = Field(min_length=1)
    sender_actor_id: str = Field(min_length=1)
    recipient_agent_id: AgentId | None = None
    purpose: CollaborationPurpose
    body: str = Field(min_length=1, max_length=32_768)
    classification: DataClassification = DataClassification.PROJECT_PRIVATE
    metadata: dict[str, str] = Field(default_factory=dict)


class AgentHealth(DomainModel):
    healthy: bool
    reason: str | None = None
