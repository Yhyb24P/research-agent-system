"""Typed command and state contracts for supervised Agent runtime instances."""

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, PositiveInt, field_validator, model_validator

from researchd.domain.base import DomainModel
from researchd.domain.ids import AgentRuntimeId, RuntimeSessionId


class LaunchMode(StrEnum):
    PROCESS = "PROCESS"
    REMOTE_HTTP = "REMOTE_HTTP"
    CLOUD = "CLOUD"
    A2A = "A2A"


class SupervisorState(StrEnum):
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    LOST = "LOST"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class ReattachState(StrEnum):
    PENDING = "PENDING"
    ATTACHED = "ATTACHED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    DETACHED = "DETACHED"
    FAILED = "FAILED"


class CommandType(StrEnum):
    START = "START"
    ATTACH = "ATTACH"
    STOP = "STOP"


class CommandStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ExternalObservation(StrEnum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"


class ProcessLaunchSpec(DomainModel):
    argv: tuple[str, ...] = Field(min_length=1, max_length=128)
    cwd: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def paths_are_absolute(self) -> "ProcessLaunchSpec":
        if not Path(self.argv[0]).is_absolute():
            raise ValueError("process executable must be an absolute path")
        if not Path(self.cwd).is_absolute():
            raise ValueError("process cwd must be an absolute path")
        if any(not argument or "\x00" in argument for argument in self.argv):
            raise ValueError("process argv contains an invalid argument")
        return self


class RemoteHttpAttachSpec(DomainModel):
    endpoint: str = Field(min_length=1, max_length=2048)
    health_path: str = Field(default="/health", min_length=1, max_length=256)

    @model_validator(mode="after")
    def endpoint_is_non_secret_and_bounded(self) -> "RemoteHttpAttachSpec":
        parsed = urlparse(self.endpoint)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("remote endpoint must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("remote endpoint must not contain query or fragment")
        loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError("remote endpoint must use HTTPS or loopback HTTP")
        if not parsed.hostname:
            raise ValueError("remote endpoint must include a host")
        if not self.health_path.startswith("/") or "?" in self.health_path or "#" in self.health_path:
            raise ValueError("health_path must be an absolute URL path")
        return self


class _RuntimeSessionCommand(DomainModel):
    command_version: Literal[1] = 1
    command_id: str = Field(min_length=1, max_length=128)
    runtime_session_id: RuntimeSessionId
    runtime_id: AgentRuntimeId
    actor_type: Literal["HUMAN", "SYSTEM"]
    actor_id: str = Field(min_length=1, max_length=128)

    @field_validator("command_id", "actor_id")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if any(ord(character) < 32 for character in value):
            raise ValueError("command identity contains control characters")
        return value

    def request_sha256(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


class RuntimeSessionStartCommand(_RuntimeSessionCommand):
    launch_spec: ProcessLaunchSpec


class RuntimeSessionAttachCommand(_RuntimeSessionCommand):
    launch_spec: RemoteHttpAttachSpec


class RuntimeSessionStopCommand(_RuntimeSessionCommand):
    expected_version: PositiveInt


class ExternalRuntimeSessionStartRequest(DomainModel):
    """Temporary external start intent; launch specs move server-side in PX00-03."""

    request_version: Literal[1] = 1
    command_id: str = Field(min_length=1, max_length=128)
    runtime_session_id: RuntimeSessionId
    runtime_id: AgentRuntimeId
    launch_spec: ProcessLaunchSpec


class ExternalRuntimeSessionAttachRequest(DomainModel):
    request_version: Literal[1] = 1
    command_id: str = Field(min_length=1, max_length=128)
    runtime_session_id: RuntimeSessionId
    runtime_id: AgentRuntimeId
    launch_spec: RemoteHttpAttachSpec


class ExternalRuntimeSessionStopRequest(DomainModel):
    request_version: Literal[1] = 1
    command_id: str = Field(min_length=1, max_length=128)
    runtime_id: AgentRuntimeId
    expected_version: PositiveInt


class RuntimeSession(DomainModel):
    runtime_session_id: RuntimeSessionId
    runtime_id: AgentRuntimeId
    launch_mode: LaunchMode
    supervisor_state: SupervisorState
    launch_spec: dict[str, object]
    external_identity: dict[str, object] | None = None
    started_at: datetime | None = None
    last_health_at: datetime | None = None
    stopped_at: datetime | None = None
    exit_reason: str | None = None
    reattach_state: ReattachState
    version: PositiveInt
    created_at: datetime
    updated_at: datetime
