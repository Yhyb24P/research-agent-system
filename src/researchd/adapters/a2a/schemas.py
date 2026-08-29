"""Strict A2A v1 ProtoJSON projections used at the researchd boundary."""

from typing import Any, Literal

from pydantic import Field, model_validator

from researchd.domain.base import DomainModel


A2A_PROTOCOL_VERSION: Literal["1.0"] = "1.0"

A2ATaskState = Literal[
    "TASK_STATE_UNSPECIFIED",
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_AUTH_REQUIRED",
]


class A2AInterface(DomainModel):
    url: str = Field(min_length=1)
    protocolBinding: Literal["HTTP+JSON", "JSONRPC", "GRPC"]
    tenant: str | None = None
    protocolVersion: Literal["1.0"] = A2A_PROTOCOL_VERSION


class A2AProvider(DomainModel):
    url: str = Field(min_length=1)
    organization: str = Field(min_length=1)


class A2ACapabilities(DomainModel):
    streaming: bool | None = None
    pushNotifications: bool | None = None
    extensions: tuple[dict[str, Any], ...] = ()
    extendedAgentCard: bool | None = None


class A2AAgentSkill(DomainModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    tags: tuple[str, ...] = Field(min_length=1)
    examples: tuple[str, ...] = ()
    inputModes: tuple[str, ...] = ()
    outputModes: tuple[str, ...] = ()
    securityRequirements: tuple[dict[str, Any], ...] = ()


class A2AAgentCard(DomainModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    supportedInterfaces: tuple[A2AInterface, ...] = Field(min_length=1)
    provider: A2AProvider | None = None
    version: str = Field(default="1.0.0", min_length=1)
    documentationUrl: str | None = None
    capabilities: A2ACapabilities
    securitySchemes: dict[str, dict[str, Any]] = Field(default_factory=dict)
    securityRequirements: tuple[dict[str, Any], ...] = ()
    defaultInputModes: tuple[str, ...] = Field(default=("application/json",), min_length=1)
    defaultOutputModes: tuple[str, ...] = Field(default=("application/json",), min_length=1)
    skills: tuple[A2AAgentSkill, ...]
    signatures: tuple[dict[str, Any], ...] = ()
    iconUrl: str | None = None


class A2APart(DomainModel):
    text: str | None = None
    raw: str | None = None
    url: str | None = None
    data: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    filename: str | None = None
    mediaType: str | None = None

    @model_validator(mode="after")
    def exactly_one_content_kind(self) -> "A2APart":
        if sum(value is not None for value in (self.text, self.raw, self.url, self.data)) != 1:
            raise ValueError("an A2A Part must contain exactly one content kind")
        return self


class A2AMessage(DomainModel):
    messageId: str = Field(min_length=1)
    contextId: str | None = None
    taskId: str | None = None
    role: Literal["ROLE_UNSPECIFIED", "ROLE_USER", "ROLE_AGENT"]
    parts: tuple[A2APart, ...] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    extensions: tuple[str, ...] = ()
    referenceTaskIds: tuple[str, ...] = ()


class A2AArtifact(DomainModel):
    artifactId: str = Field(min_length=1)
    name: str | None = None
    description: str | None = None
    parts: tuple[A2APart, ...] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    extensions: tuple[str, ...] = ()


class A2ATaskStatus(DomainModel):
    state: A2ATaskState
    message: A2AMessage | None = None
    timestamp: str | None = None


class A2ATask(DomainModel):
    id: str = Field(min_length=1)
    contextId: str | None = None
    status: A2ATaskStatus
    artifacts: tuple[A2AArtifact, ...] = ()
    history: tuple[A2AMessage, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class A2ASendMessageRequest(DomainModel):
    tenant: str | None = None
    message: A2AMessage
    configuration: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class A2ATaskStatusUpdate(DomainModel):
    taskId: str = Field(min_length=1)
    contextId: str = Field(min_length=1)
    status: A2ATaskStatus
    metadata: dict[str, Any] = Field(default_factory=dict)


class A2ATaskArtifactUpdate(DomainModel):
    taskId: str = Field(min_length=1)
    contextId: str = Field(min_length=1)
    artifact: A2AArtifact
    append: bool = False
    lastChunk: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
