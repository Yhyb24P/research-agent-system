from enum import StrEnum


class DataClassification(StrEnum):
    PUBLIC = "PUBLIC"
    CLOUD_SAFE = "CLOUD_SAFE"
    PROJECT_PRIVATE = "PROJECT_PRIVATE"
    LOCAL_ONLY = "LOCAL_ONLY"
    SECRET = "SECRET"


class Capability(StrEnum):
    WORKSPACE_READ = "workspace.read"
    WORKSPACE_WRITE = "workspace.write"
    SANDBOX_SHELL = "sandbox.shell"
    GIT_DIFF = "git.diff"
    GIT_STATUS = "git.status"
    TEST_RUN = "test.run"
    PYTHON_RUN = "python.run"
    JOB_SUBMIT_GPU = "job.submit.gpu"
    JOB_CANCEL = "job.cancel"
    NETWORK_EXTERNAL = "network.external"
    ARTIFACT_CREATE = "artifact.create"
    ARTIFACT_DERIVE = "artifact.derive"
    GIT_COMMIT = "git.commit"
    GIT_PUSH = "git.push"
    PACKAGE_INSTALL_SYSTEM = "package.install.system"


class NetworkMode(StrEnum):
    NONE = "none"
    RESTRICTED = "restricted"
    FULL = "full"


class ResearchRunState(StrEnum):
    NEW = "NEW"
    PLANNING = "PLANNING"
    ACTIVE = "ACTIVE"
    REVIEWING = "REVIEWING"
    WAITING_HUMAN = "WAITING_HUMAN"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class WorkOrderState(StrEnum):
    DRAFT = "DRAFT"
    POLICY_CHECK = "POLICY_CHECK"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    READY = "READY"
    DISPATCHED = "DISPATCHED"
    EXECUTING = "EXECUTING"
    WAITING_JOB = "WAITING_JOB"
    VERIFYING = "VERIFYING"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    REVIEW_READY = "REVIEW_READY"
    REVIEWING = "REVIEWING"
    ACCEPTED = "ACCEPTED"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    MORE_EVIDENCE_REQUIRED = "MORE_EVIDENCE_REQUIRED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AttemptState(StrEnum):
    CREATED = "CREATED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    WAITING_JOB = "WAITING_JOB"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobState(StrEnum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    LOST = "LOST"


class VerificationOverall(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"


class CriterionResult(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"


class ReviewDecisionKind(StrEnum):
    ACCEPT = "ACCEPT"
    REVISE = "REVISE"
    MORE_EVIDENCE = "MORE_EVIDENCE"
    REPLAN = "REPLAN"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    ABORT_RECOMMENDED = "ABORT_RECOMMENDED"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class PolicyOutcome(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"


class AgentTrustZone(StrEnum):
    LOCAL_PRIVATE = "LOCAL_PRIVATE"
    REMOTE_PRIVATE = "REMOTE_PRIVATE"
    EXTERNAL_CLOUD = "EXTERNAL_CLOUD"
    EXTERNAL_UNTRUSTED = "EXTERNAL_UNTRUSTED"


class AgentAdapterKind(StrEnum):
    INTERNAL = "INTERNAL"
    PROCESS = "PROCESS"
    HTTP = "HTTP"
    A2A = "A2A"


class DelegationPurpose(StrEnum):
    PLAN = "PLAN"
    EXECUTE = "EXECUTE"
    REVIEW = "REVIEW"
    EVIDENCE = "EVIDENCE"
    SPECIALIST = "SPECIALIST"


class DelegationState(StrEnum):
    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class InvocationStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
