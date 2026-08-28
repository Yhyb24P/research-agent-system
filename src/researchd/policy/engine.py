from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from researchd.domain.enums import Capability, DataClassification, PolicyOutcome
from researchd.storage.models import PolicyDecisionRecord
from researchd.storage.repositories import utc_now


@dataclass(frozen=True)
class BudgetLimits:
    max_wall_seconds: int
    max_cpu_seconds: int
    max_gpu_seconds: int
    max_disk_mb: int
    max_output_mb: int


@dataclass(frozen=True)
class PolicyRequest:
    requested_capabilities: frozenset[Capability]
    workspace_capabilities: frozenset[Capability]
    user_capabilities: frozenset[Capability]
    approved_capabilities: frozenset[Capability]
    requested_budget: BudgetLimits
    maximum_budget: BudgetLimits
    data_classification: DataClassification | str


@dataclass(frozen=True)
class PolicyDecision:
    outcome: PolicyOutcome
    effective_capabilities: tuple[Capability, ...]
    reason_codes: tuple[str, ...]


class PolicyEvaluator(Protocol):
    def evaluate(self, request: PolicyRequest) -> PolicyDecision: ...


class DeterministicPolicyEngine:
    approval_required = frozenset({
        Capability.GIT_PUSH,
        Capability.PACKAGE_INSTALL_SYSTEM,
        Capability.NETWORK_EXTERNAL,
        Capability.JOB_SUBMIT_GPU,
    })

    def evaluate(self, request: PolicyRequest) -> PolicyDecision:
        reasons: set[str] = set()
        try:
            classification = DataClassification(request.data_classification)
        except ValueError:
            return PolicyDecision(PolicyOutcome.DENY, (), ("POLICY_DENY_UNKNOWN_CLASSIFICATION",))

        allowed = request.workspace_capabilities & request.user_capabilities
        missing = request.requested_capabilities - allowed
        if missing:
            reasons.add("POLICY_DENY_CAPABILITY")

        needs_approval = request.requested_capabilities & self.approval_required - request.approved_capabilities
        if needs_approval:
            reasons.add("POLICY_APPROVAL_REQUIRED")

        if classification is DataClassification.SECRET and Capability.NETWORK_EXTERNAL in request.requested_capabilities:
            reasons.add("POLICY_DENY_SECRET")

        for field in BudgetLimits.__dataclass_fields__:
            if getattr(request.requested_budget, field) > getattr(request.maximum_budget, field):
                reasons.add("POLICY_DENY_BUDGET")

        effective = request.requested_capabilities & allowed
        effective -= needs_approval
        if any(reason.startswith("POLICY_DENY") for reason in reasons):
            outcome = PolicyOutcome.DENY
            effective = frozenset()
        elif needs_approval:
            outcome = PolicyOutcome.APPROVAL_REQUIRED
        else:
            outcome = PolicyOutcome.ALLOW
        return PolicyDecision(outcome, tuple(sorted(effective, key=str)), tuple(sorted(reasons)))


class RecordingPolicyEngine:
    def __init__(self, engine: PolicyEvaluator, sessions: sessionmaker[Session]) -> None:
        self.engine = engine
        self.sessions = sessions

    def evaluate_and_record(self, run_id: str, work_order_id: str | None, request: PolicyRequest) -> PolicyDecision:
        decision = self.engine.evaluate(request)
        with self.sessions.begin() as session:
            session.add(PolicyDecisionRecord(
                policy_decision_id=f"pol_{uuid4().hex}", run_id=run_id,
                work_order_id=work_order_id, outcome=decision.outcome.value,
                reason_codes=list(decision.reason_codes),
                requested_capabilities=sorted(capability.value for capability in request.requested_capabilities),
                effective_capabilities=[capability.value for capability in decision.effective_capabilities],
                created_at=utc_now(),
            ))
        return decision
