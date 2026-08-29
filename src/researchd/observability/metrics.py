"""Structured, privacy-preserving operational metrics from authoritative records."""

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.storage.models import (
    AgentInteractionRecord, ApprovalRequestRecord, JobRecord, PolicyDecisionRecord,
    ReviewDecisionRecord, VerificationResultRecord, AttemptRecord, WorkOrderRecord,
    DelegationRecord, AgentInvocationRecord,
    AgentRecord, AgentRuntimeRecord,
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
    delegations: dict[str, int]
    invocations: dict[str, int]
    agent_invocation_failures: int
    agent_utilization: dict[str, float]
    agent_runtime_health: dict[str, int]

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
            "delegations": dict(self.delegations), "invocations": dict(self.invocations),
            "agent_invocation_failures": self.agent_invocation_failures,
            "agent_utilization": dict(self.agent_utilization),
            "agent_runtime_health": dict(self.agent_runtime_health),
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
        for key, value in sorted(self.delegations.items()):
            purpose, state = key.split(":", 1)
            lines.append(f'research_delegations_total{{purpose="{purpose}",state="{state}"}} {value}')
        for status, value in sorted(self.invocations.items()):
            lines.append(f'research_agent_invocations_total{{status="{status}"}} {value}')
        lines.append(f"research_agent_invocation_failures_total {self.agent_invocation_failures}")
        for agent_id, utilization_value in sorted(self.agent_utilization.items()):
            lines.append(f'research_agent_utilization_ratio{{agent_id="{agent_id}"}} {utilization_value}')
        for runtime_id, health_value in sorted(self.agent_runtime_health.items()):
            lines.append(f'research_agent_runtime_health{{runtime_id="{runtime_id}"}} {health_value}')
        return "\n".join(lines) + "\n"


def collect_metrics(sessions: sessionmaker[Session], *, run_id: str | None = None) -> MetricsSnapshot:
    with sessions() as session:
        interaction_query = select(AgentInteractionRecord)
        job_query = select(JobRecord)
        policy_query = select(PolicyDecisionRecord)
        approval_query = select(ApprovalRequestRecord)
        verifier_query = select(VerificationResultRecord)
        review_query = select(ReviewDecisionRecord)
        delegation_query = select(DelegationRecord)
        invocation_query = select(AgentInvocationRecord)
        if run_id is not None:
            interaction_query = interaction_query.where(AgentInteractionRecord.run_id == run_id)
            job_query = job_query.join(AttemptRecord, AttemptRecord.attempt_id == JobRecord.attempt_id).join(WorkOrderRecord, WorkOrderRecord.work_order_id == AttemptRecord.work_order_id).where(WorkOrderRecord.run_id == run_id)
            policy_query = policy_query.where(PolicyDecisionRecord.run_id == run_id)
            approval_query = approval_query.where(ApprovalRequestRecord.run_id == run_id)
            verifier_query = verifier_query.join(WorkOrderRecord, WorkOrderRecord.work_order_id == VerificationResultRecord.work_order_id).where(WorkOrderRecord.run_id == run_id)
            review_query = review_query.where(ReviewDecisionRecord.run_id == run_id)
            delegation_query = delegation_query.where(DelegationRecord.run_id == run_id)
            invocation_query = invocation_query.where(AgentInvocationRecord.run_id == run_id)
        interactions = session.scalars(interaction_query).all()
        jobs = session.scalars(job_query).all()
        policies = session.scalars(policy_query).all()
        approvals = session.scalars(approval_query).all()
        verifications = session.scalars(verifier_query).all()
        reviews = session.scalars(review_query).all()
        delegations = session.scalars(delegation_query).all()
        invocations = session.scalars(invocation_query).all()
        agents = session.scalars(select(AgentRecord).order_by(AgentRecord.agent_id)).all()
        runtimes = session.scalars(select(AgentRuntimeRecord).order_by(AgentRuntimeRecord.runtime_id)).all()
        cloud_statuses = Counter(item.status for item in interactions)
        active_by_agent = Counter(item.assigned_agent_id for item in delegations if item.state in {"ASSIGNED", "RUNNING"} and item.assigned_agent_id is not None)
        utilization = {agent.agent_id: round(active_by_agent.get(agent.agent_id, 0) / agent.max_parallel_delegations, 6) for agent in agents}
        reference = datetime.now(UTC)
        runtime_health = {runtime.runtime_id: int(runtime.enabled and runtime.lease_expires_at is not None and runtime.lease_expires_at > reference and any(agent.agent_id == runtime.agent_id and agent.enabled for agent in agents)) for runtime in runtimes}
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
            delegations=dict(Counter(f"{item.purpose}:{item.state}" for item in delegations)),
            invocations=dict(Counter(item.status for item in invocations)),
            agent_invocation_failures=sum(item.status == "FAILED" for item in invocations),
            agent_utilization=utilization,
            agent_runtime_health=runtime_health,
        )
