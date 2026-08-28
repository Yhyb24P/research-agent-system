"""Small, dependency-free A2A 1.0 wire models used at the boundary."""

from typing import Any, Literal

from pydantic import Field

from researchd.domain.base import DomainModel


A2A_PROTOCOL_VERSION = "1.0.0"


class A2AInterface(DomainModel):
    url: str
    protocolBinding: Literal["HTTP+JSON", "JSONRPC"]
    protocolVersion: str = A2A_PROTOCOL_VERSION


class A2AAgentCard(DomainModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    url: str
    version: str = "1.0.0"
    protocolVersion: str = A2A_PROTOCOL_VERSION
    supportedInterfaces: tuple[A2AInterface, ...] = Field(min_length=1)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    skills: tuple[dict[str, Any], ...] = ()
    securitySchemes: dict[str, Any] = Field(default_factory=dict)


class A2ATaskStatus(DomainModel):
    state: Literal["submitted", "working", "input-required", "completed", "failed", "canceled", "rejected"]
    message: str | None = None


class A2ATask(DomainModel):
    id: str = Field(min_length=1)
    contextId: str | None = None
    status: A2ATaskStatus
    artifacts: tuple[dict[str, Any], ...] = ()
    history: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
