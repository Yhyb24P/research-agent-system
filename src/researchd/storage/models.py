from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from researchd.storage.types import UTCDateTime


class Base(DeclarativeBase):
    pass


class VersionedTimestamps:
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class WorkspaceRecord(Base, VersionedTimestamps):
    __tablename__ = "workspaces"
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)


class ResearchRunRecord(Base, VersionedTimestamps):
    __tablename__ = "research_runs"
    __table_args__ = (Index("ix_research_runs_state", "state"),)
    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.workspace_id"), nullable=False, index=True)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    max_iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    max_cloud_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    iterations_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cloud_calls_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancellation_requested: Mapped[bool] = mapped_column(nullable=False, default=False)


class PlanRecord(Base, VersionedTimestamps):
    __tablename__ = "plans"
    __table_args__ = (Index("ix_plans_run_created", "run_id", "created_at"),)
    plan_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), nullable=False)
    proposal_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class WorkOrderRecord(Base, VersionedTimestamps):
    __tablename__ = "work_orders"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_work_orders_idempotency_key"),
        Index("ix_work_orders_state", "state"),
        Index("ix_work_orders_run_state", "run_id", "state"),
    )
    work_order_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), nullable=False)
    parent_work_order_id: Mapped[str | None] = mapped_column(ForeignKey("work_orders.work_order_id"))
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    contract: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    revision_reason: Mapped[str | None] = mapped_column(Text)
    approval_id: Mapped[str | None] = mapped_column(String(128))
    approval_grant_id: Mapped[str | None] = mapped_column(String(128))


class AttemptRecord(Base, VersionedTimestamps):
    __tablename__ = "attempts"
    __table_args__ = (
        Index("ix_attempts_state", "state"),
        Index("ix_attempts_work_order_state", "work_order_id", "state"),
    )
    attempt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    work_order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.work_order_id"), nullable=False)
    delegation_id: Mapped[str | None] = mapped_column(ForeignKey("delegations.delegation_id"))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class JobRecord(Base, VersionedTimestamps):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_jobs_operation_id"),
        Index("ix_jobs_state", "state"),
        Index("ix_jobs_attempt_state", "attempt_id", "state"),
    )
    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.attempt_id"), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    backend: Mapped[str] = mapped_column(String(64), nullable=False)
    native_handle: Mapped[str | None] = mapped_column(String(256))


class GpuLeaseRecord(Base):
    __tablename__ = "gpu_leases"
    __table_args__ = (
        UniqueConstraint("job_id", "device_id", name="uq_gpu_leases_job_device"),
        Index("ix_gpu_leases_device_state", "device_id", "state"),
        Index("ix_gpu_leases_job_state", "job_id", "state"),
    )
    lease_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), nullable=False)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AgentRecord(Base, VersionedTimestamps):
    __tablename__ = "agents"
    __table_args__ = (Index("ix_agents_enabled", "enabled"),)
    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    roles_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    skills_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    trust_zone: Mapped[str] = mapped_column(String(32), nullable=False)
    constraints_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    labels_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    max_parallel_delegations: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class AgentRuntimeRecord(Base, VersionedTimestamps):
    __tablename__ = "agent_runtimes"
    __table_args__ = (Index("ix_agent_runtimes_agent", "agent_id"), Index("ix_agent_runtimes_lease", "lease_expires_at"))
    runtime_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id"), nullable=False)
    adapter_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    runtime_name: Mapped[str] = mapped_column(String(256), nullable=False)
    endpoint_ref: Mapped[str | None] = mapped_column(String(512))
    framework: Mapped[str | None] = mapped_column(String(128))
    model_provider: Mapped[str | None] = mapped_column(String(128))
    model_name: Mapped[str | None] = mapped_column(String(256))
    protocols_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    metadata_json: Mapped[dict[str, str]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class DelegationRecord(Base, VersionedTimestamps):
    __tablename__ = "delegations"
    __table_args__ = (Index("ix_delegations_run_state", "run_id", "state"), Index("ix_delegations_idempotency", "idempotency_key", unique=True))
    delegation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), nullable=False)
    work_order_id: Mapped[str | None] = mapped_column(ForeignKey("work_orders.work_order_id"))
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    required_roles_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    required_skills_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    required_trust_zones_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    assigned_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.agent_id"))
    assigned_runtime_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runtimes.runtime_id"))
    agent_profile_version: Mapped[int | None] = mapped_column(Integer)
    agent_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    assignment_sha256: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AgentInvocationRecord(Base):
    __tablename__ = "agent_invocations"
    __table_args__ = (Index("ix_agent_invocations_delegation", "delegation_id"), Index("ix_agent_invocations_status", "status"))
    invocation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    delegation_id: Mapped[str] = mapped_column(ForeignKey("delegations.delegation_id"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), nullable=False)
    work_order_id: Mapped[str | None] = mapped_column(ForeignKey("work_orders.work_order_id"))
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("attempts.attempt_id"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id"), nullable=False)
    runtime_id: Mapped[str] = mapped_column(ForeignKey("agent_runtimes.runtime_id"), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    context_bundle_sha256: Mapped[str | None] = mapped_column(String(64))
    context_bundle_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    output_type: Mapped[str | None] = mapped_column(String(128))
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reason_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class CollaborationMessageRecord(Base):
    __tablename__ = "collaboration_messages"
    __table_args__ = (Index("ix_collaboration_messages_run_created", "run_id", "created_at"),)
    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), nullable=False)
    sender_actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    sender_actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    recipient_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.agent_id"))
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, str]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ArtifactRecord(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint("size >= 0", name="ck_artifacts_size_nonnegative"),
        Index("ix_artifacts_attempt_id", "attempt_id"),
        Index("ix_artifacts_classification", "classification"),
    )
    artifact_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(256), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    classification: Mapped[str] = mapped_column(String(32), nullable=False)
    producer_type: Mapped[str] = mapped_column(String(32), nullable=False)
    producer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("attempts.attempt_id"))
    relative_source_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AuditEventRecord(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("length(event_type) > 0", name="ck_audit_events_event_type_nonempty"),
        Index("ix_audit_events_run_timestamp", "run_id", "timestamp"),
        Index("ix_audit_events_entity", "entity_type", "entity_id"),
        Index("ix_audit_events_correlation_id", "correlation_id"),
    )
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    causation_id: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)


class ArtifactDerivationRecord(Base):
    __tablename__ = "artifact_derivations"
    __table_args__ = (Index("ix_artifact_derivations_source", "source_artifact_id"),)
    derived_artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.artifact_id"), primary_key=True)
    source_artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.artifact_id"), primary_key=True)
    producer: Mapped[str] = mapped_column(String(128), nullable=False)
    producer_version: Mapped[str] = mapped_column(String(128), nullable=False)
    parameters_json: Mapped[dict[str, Any]] = mapped_column("parameters", JSON, nullable=False)
    parameters_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    transformation_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ApprovalRequestRecord(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (Index("ix_approval_requests_status_expires", "status", "expires_at"),)
    approval_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("research_runs.run_id"))
    work_order_id: Mapped[str | None] = mapped_column(ForeignKey("work_orders.work_order_id"))
    requester_actor_type: Mapped[str] = mapped_column(String(32), nullable=False, default="legacy")
    requester_actor_id: Mapped[str | None] = mapped_column(String(128))
    operation_type: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_parameters: Mapped[str] = mapped_column(Text, nullable=False)
    parameter_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    budget_delta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    one_shot: Mapped[bool] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ApprovalGrantRecord(Base):
    __tablename__ = "approval_grants"
    __table_args__ = (Index("ix_approval_grants_hash_expires", "parameter_sha256", "expires_at"),)
    grant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    approval_id: Mapped[str] = mapped_column(ForeignKey("approval_requests.approval_id"), nullable=False, unique=True)
    parameter_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    granted_by: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    one_shot: Mapped[bool] = mapped_column(nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class PolicyDecisionRecord(Base):
    __tablename__ = "policy_decisions"
    __table_args__ = (Index("ix_policy_decisions_run_created", "run_id", "created_at"),)
    policy_decision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), nullable=False)
    work_order_id: Mapped[str | None] = mapped_column(ForeignKey("work_orders.work_order_id"))
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    requested_capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    effective_capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ExecutionStepRecord(Base):
    __tablename__ = "execution_steps"
    __table_args__ = (Index("ix_execution_steps_attempt_status", "attempt_id", "status"),)
    step_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.attempt_id"), nullable=False)
    capability: Mapped[str] = mapped_column(String(128), nullable=False)
    parameters_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ExecutorDispatchRecord(Base):
    __tablename__ = "executor_dispatches"
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.attempt_id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AttemptWorktreeRecord(Base):
    __tablename__ = "attempt_worktrees"
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.attempt_id"), primary_key=True)
    repository_id: Mapped[str] = mapped_column(String(128), nullable=False)
    base_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    worktree_path: Mapped[str] = mapped_column(Text, nullable=False)
    environment_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    sandbox_backend: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ObservationRecord(Base):
    __tablename__ = "observations"
    __table_args__ = (
        CheckConstraint(
            "json_array_length(source_artifact_ids) + json_array_length(source_step_ids) + json_array_length(source_job_ids) > 0",
            name="ck_observations_has_source",
        ),
        Index("ix_observations_attempt_name", "attempt_id", "name"),
    )
    observation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.attempt_id"), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    value_json: Mapped[Any] = mapped_column("value", JSON, nullable=False)
    source_artifact_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_step_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_job_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    producer_type: Mapped[str] = mapped_column(String(64), nullable=False)
    producer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    producer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    classification: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ClaimRecord(Base):
    __tablename__ = "claims"
    __table_args__ = (Index("ix_claims_attempt_id", "attempt_id"),)
    claim_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.attempt_id"), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    producer_type: Mapped[str] = mapped_column(String(64), nullable=False)
    producer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class VerificationResultRecord(Base):
    __tablename__ = "verification_results"
    __table_args__ = (
        Index("ix_verification_results_work_order_created", "work_order_id", "created_at"),
        Index("ix_verification_results_attempt_created", "attempt_id", "created_at"),
    )
    verification_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("attempts.attempt_id"), nullable=False)
    work_order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.work_order_id"), nullable=False)
    overall: Mapped[str] = mapped_column(String(32), nullable=False)
    criteria_json: Mapped[list[dict[str, Any]]] = mapped_column("criteria", JSON, nullable=False)
    acceptance_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    verifier_version: Mapped[str] = mapped_column(String(64), nullable=False)
    valid: Mapped[bool] = mapped_column(nullable=False)
    classification: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ReviewDecisionRecord(Base):
    __tablename__ = "review_decisions"
    __table_args__ = (
        Index("ix_review_decisions_run_created", "run_id", "created_at"),
        Index("ix_review_decisions_work_order_created", "work_order_id", "created_at"),
    )
    review_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), nullable=False)
    work_order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.work_order_id"), nullable=False)
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("attempts.attempt_id"))
    interaction_id: Mapped[str | None] = mapped_column(ForeignKey("agent_interactions.interaction_id"))
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    deficiencies: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    requested_next_objective: Mapped[str | None] = mapped_column(Text)
    requested_evidence: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AgentInteractionRecord(Base):
    __tablename__ = "agent_interactions"
    __table_args__ = (
        Index("ix_agent_interactions_run_created", "run_id", "created_at"),
        Index("ix_agent_interactions_status", "status"),
        Index("ix_agent_interactions_a2a_task", "a2a_task_id"),
    )
    interaction_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    invocation_id: Mapped[str | None] = mapped_column(ForeignKey("agent_invocations.invocation_id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id"), nullable=False)
    work_order_id: Mapped[str | None] = mapped_column(ForeignKey("work_orders.work_order_id"))
    attempt_id: Mapped[str | None] = mapped_column(String(128))
    remote_agent_id: Mapped[str | None] = mapped_column(String(256))
    a2a_context_id: Mapped[str | None] = mapped_column(String(256))
    a2a_task_id: Mapped[str | None] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    bundle_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    response_type: Mapped[str] = mapped_column(String(128), nullable=False)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(128))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_usd: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
