from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from researchd.domain.base import DomainModel
from researchd.domain.enums import Capability, NetworkMode


class CommandLimits(DomainModel):
    wall_seconds: float = Field(gt=0, le=3600)
    cpu_seconds: int = Field(gt=0, le=3600)
    memory_mb: int = Field(gt=0)
    file_size_mb: int = Field(gt=0)
    output_bytes: int = Field(gt=0)
    terminate_grace_seconds: float = Field(default=1.0, ge=0, le=10)


class SandboxMount(DomainModel):
    source: str
    target: str
    read_only: bool = True


class SandboxSpec(DomainModel):
    attempt_id: str
    workspace: str
    network: NetworkMode = NetworkMode.NONE
    mounts: tuple[SandboxMount, ...] = ()
    environment: dict[str, str] = Field(default_factory=dict)


class CommandSpec(DomainModel):
    execution_id: str = Field(default_factory=lambda: f"exec_{uuid4().hex}")
    argv: tuple[str, ...]
    cwd: str = "/workspace"
    limits: CommandLimits

    @field_validator("argv")
    @classmethod
    def argv_cannot_be_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item or "\x00" in item for item in value):
            raise ValueError("argv must contain nonempty NUL-free arguments")
        return value


class CommandResult(DomainModel):
    execution_id: str
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    cancelled: bool
    output_limit_exceeded: bool
    duration_seconds: float


class CapabilityRequest(DomainModel):
    request_id: str
    capability: Capability
    parameters: dict[str, Any]


class CapabilityResult(DomainModel):
    request_id: str
    status: Literal["ok", "failed", "denied"]
    exit_code: int | None = None
    output: str = ""
    output_artifact_id: str | None = None
    reason_code: str | None = None


class GrantedWorkOrder(DomainModel):
    attempt_id: str
    objective: str
    granted_capabilities: frozenset[Capability]
    sandbox: SandboxSpec
    max_agent_steps: int = Field(default=16, gt=0, le=100)


class LocalAgentRequest(DomainModel):
    objective: str
    prior_results: tuple[CapabilityResult, ...]
    granted_capabilities: frozenset[Capability]


class LocalAgentResponse(DomainModel):
    actions: tuple[CapabilityRequest, ...] = ()
    final_claim: str | None = None


class ExecutorResult(DomainModel):
    attempt_id: str
    status: Literal["execution_complete", "failed", "model_unavailable", "step_limit"]
    capability_results: tuple[CapabilityResult, ...]
    reported_claims: tuple[str, ...]
    errors: tuple[str, ...]


class JobResources(DomainModel):
    gpu_count: int = Field(default=0, ge=0)
    max_gpu_seconds: int = Field(default=0, ge=0)
    cpu_count: int = Field(default=1, gt=0)
    memory_mb: int = Field(gt=0)


class JobSpec(DomainModel):
    job_type: str
    attempt_id: str
    backend: Literal["local"] = "local"
    resources: JobResources
    network: NetworkMode = NetworkMode.NONE
    operation_id: str = Field(min_length=8)
    workspace: str
    inputs: tuple[str, ...] = ()
    # Populated by the trusted GPU admission controller immediately before
    # backend submission; callers must not use this as an authorization input.
    gpu_device_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def gpu_job_requires_time_budget(self) -> "JobSpec":
        if self.resources.gpu_count and self.resources.max_gpu_seconds <= 0:
            raise ValueError("GPU jobs require a positive max_gpu_seconds budget")
        return self


class JobHandle(DomainModel):
    native_handle: str
    state: str
