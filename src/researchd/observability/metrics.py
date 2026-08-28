"""Structured, privacy-preserving operational metrics from authoritative records."""

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.storage.models import (
    AgentInteractionRecord, ApprovalRequestRecord, JobRecord, PolicyDecisionRecord,
    ReviewDecisionRecord, VerificationResultRecord, AttemptRecord, WorkOrderRecord,
)


@dataclass(frozen=True)
class MetricsSnapshot:
    cloud_calls: int
    cloud_tokens: int
    cloud_cost_usd: Decimal
    cloud_statuses: dict[str, int]
    job_states: dict[str, int]
    policy_outcomes: dict[str, int]
    approval_statuses: dict[str, int]
    verifier_outcomes: dict[str, int]
    review_decisions: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "cloud_calls": self.cloud_calls,
            "cloud_tokens": self.cloud_tokens,
            "cloud_cost_usd": format(self.cloud_cost_usd, "f"),
            "cloud_statuses": dict(self.cloud_statuses),
            "job_states": dict(self.job_states),
            "policy_outcomes": dict(self.policy_outcomes),
            "approval_statuses": dict(self.approval_statuses),
            "verifier_outcomes": dict(self.verifier_outcomes),
            "review_decisions": dict(self.review_decisions),
        }

    def prometheus(self) -> str:
        lines = [
            f"research_cloud_calls_total {self.cloud_calls}",
            f"research_cloud_tokens_total {self.cloud_tokens}",
            f"research_cloud_cost_usd_total {self.cloud_cost_usd}",
        ]
        for name, values in (
            ("cloud_status", self.cloud_statuses), ("job_state", self.job_states),
            ("policy_outcome", self.policy_outcomes), ("approval_status", self.approval_statuses),
            ("verifier_outcome", self.verifier_outcomes), ("review_decision", self.review_decisions),
        ):
            for label, value in sorted(values.items()):
                safe_label = label.replace('"', "")
                lines.append(f'research_{name}_total{{state="{safe_label}"}} {value}')
        return "\n".join(lines) + "\n"


def collect_metrics(sessions: sessionmaker[Session], *, run_id: str | None = None) -> MetricsSnapshot:
    with sessions() as session:
        interaction_query = select(AgentInteractionRecord)
        job_query = select(JobRecord)
        policy_query = select(PolicyDecisionRecord)
        approval_query = select(ApprovalRequestRecord)
        verifier_query = select(VerificationResultRecord)
        review_query = select(ReviewDecisionRecord)
        if run_id is not None:
            interaction_query = interaction_query.where(AgentInteractionRecord.run_id == run_id)
            job_query = job_query.join(AttemptRecord, AttemptRecord.attempt_id == JobRecord.attempt_id).join(WorkOrderRecord, WorkOrderRecord.work_order_id == AttemptRecord.work_order_id).where(WorkOrderRecord.run_id == run_id)
            policy_query = policy_query.where(PolicyDecisionRecord.run_id == run_id)
            approval_query = approval_query  # approval records have no run FK in V1
            verifier_query = verifier_query.join(WorkOrderRecord, WorkOrderRecord.work_order_id == VerificationResultRecord.work_order_id).where(WorkOrderRecord.run_id == run_id)
            review_query = review_query.where(ReviewDecisionRecord.run_id == run_id)
        interactions = session.scalars(interaction_query).all()
        jobs = session.scalars(job_query).all()
        policies = session.scalars(policy_query).all()
        approvals = session.scalars(approval_query).all()
        verifications = session.scalars(verifier_query).all()
        reviews = session.scalars(review_query).all()
        cloud_statuses = Counter(item.status for item in interactions)
        return MetricsSnapshot(
            cloud_calls=len(interactions),
            cloud_tokens=sum(item.total_tokens for item in interactions),
            cloud_cost_usd=sum((Decimal(item.cost_usd) for item in interactions), Decimal("0")),
            cloud_statuses=dict(cloud_statuses),
            job_states=dict(Counter(item.state for item in jobs)),
            policy_outcomes=dict(Counter(item.outcome for item in policies)),
            approval_statuses=dict(Counter(item.status for item in approvals)),
            verifier_outcomes=dict(Counter(item.overall for item in verifications)),
            review_decisions=dict(Counter(item.decision for item in reviews)),
        )
