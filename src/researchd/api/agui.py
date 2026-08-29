"""Read-only AG-UI event projection over the authoritative audit stream."""

from datetime import datetime
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from researchd.api.control import LocalControlAPI


class _AGUIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class RunStartedEvent(_AGUIModel):
    type: Literal["RUN_STARTED"] = "RUN_STARTED"
    thread_id: str = Field(alias="threadId")
    run_id: str = Field(alias="runId")
    timestamp: int


class RunFinishedEvent(_AGUIModel):
    type: Literal["RUN_FINISHED"] = "RUN_FINISHED"
    thread_id: str = Field(alias="threadId")
    run_id: str = Field(alias="runId")
    result: dict[str, Any]
    timestamp: int


class RunErrorEvent(_AGUIModel):
    type: Literal["RUN_ERROR"] = "RUN_ERROR"
    message: str
    code: str
    timestamp: int


class StateSnapshotEvent(_AGUIModel):
    type: Literal["STATE_SNAPSHOT"] = "STATE_SNAPSHOT"
    snapshot: dict[str, Any]
    timestamp: int


class StateDeltaEvent(_AGUIModel):
    type: Literal["STATE_DELTA"] = "STATE_DELTA"
    delta: list[dict[str, Any]]
    timestamp: int


class ActivitySnapshotEvent(_AGUIModel):
    type: Literal["ACTIVITY_SNAPSHOT"] = "ACTIVITY_SNAPSHOT"
    message_id: str = Field(alias="messageId")
    activity_type: str = Field(alias="activityType")
    content: dict[str, Any]
    replace: bool = True
    timestamp: int


class TextMessageChunkEvent(_AGUIModel):
    type: Literal["TEXT_MESSAGE_CHUNK"] = "TEXT_MESSAGE_CHUNK"
    message_id: str = Field(alias="messageId")
    role: Literal["user", "assistant", "system", "tool"]
    delta: str
    timestamp: int


class CustomEvent(_AGUIModel):
    type: Literal["CUSTOM"] = "CUSTOM"
    name: str
    value: dict[str, Any]
    timestamp: int


AGUIEvent = Annotated[
    RunStartedEvent
    | RunFinishedEvent
    | RunErrorEvent
    | StateSnapshotEvent
    | StateDeltaEvent
    | ActivitySnapshotEvent
    | TextMessageChunkEvent
    | CustomEvent,
    Field(discriminator="type"),
]


class ProjectedEvent(_AGUIModel):
    stream_offset: int
    event: AGUIEvent

    def as_sse(self) -> bytes:
        body = json.dumps(
            self.event.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return f"id: {self.stream_offset}\nevent: ag-ui\ndata: {body}\n\n".encode()


def _timestamp_ms(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp() * 1000)


class AGUIProjectionAdapter:
    """Maps durable controller records to presentation-only AG-UI events."""

    _ACTIVITY_PREFIXES = ("ATTEMPT_", "WORKSPACE_", "EXECUTION_", "RECOVERY_")
    _CUSTOM_EVENTS = {
        "APPROVAL_REQUESTED": "researchd.approval.requested",
        "APPROVAL_GRANTED": "researchd.approval.granted",
        "VERIFICATION_STARTED": "researchd.verification.started",
        "VERIFICATION_COMPLETED": "researchd.verification.completed",
        "VERIFICATION_FAILED": "researchd.verification.failed",
        "HUMAN_REQUIRED": "researchd.human.required",
        "HUMAN_ABORTED": "researchd.human.aborted",
        "HUMAN_REVISION_REQUESTED": "researchd.human.revision_requested",
    }
    _RUN_FAILURE_EVENTS = {
        "RUN_FAILED",
        "MAX_WALL_TIME_EXCEEDED",
        "CLOUD_REVIEW_FAILED",
        "REVIEW_SCOPE_MISMATCH",
        "REVIEW_EVIDENCE_INCOMPLETE",
        "ABORT_RECOMMENDED",
        "HUMAN_ABORTED",
    }

    def __init__(self, api: LocalControlAPI) -> None:
        self.api = api

    def snapshot(self, run_id: str) -> ProjectedEvent:
        return ProjectedEvent(
            stream_offset=0,
            event=StateSnapshotEvent(
                snapshot=self.api.stream_snapshot(run_id),
                timestamp=int(datetime.now().timestamp() * 1000),
            ),
        )

    def replay(self, run_id: str, *, after_stream_offset: int | None = None) -> list[ProjectedEvent]:
        projected: list[ProjectedEvent] = []
        if after_stream_offset is None:
            projected.append(self.snapshot(run_id))
        for record in self.api.events(run_id, after_stream_offset=after_stream_offset):
            offset = record["stream_offset"]
            if not isinstance(offset, int):
                raise RuntimeError("audit stream event has no durable offset")
            projected.append(ProjectedEvent(stream_offset=offset, event=self.project(record, run_id=run_id)))
        return projected

    def project(self, record: dict[str, Any], *, run_id: str) -> AGUIEvent:
        event_type = str(record["event_type"])
        timestamp = _timestamp_ms(str(record["timestamp"]))
        if event_type == "RUN_CREATED":
            return RunStartedEvent(threadId=run_id, runId=run_id, timestamp=timestamp)
        if event_type in {"RUN_COMPLETED", "RUN_CANCELLED"}:
            return RunFinishedEvent(
                threadId=run_id,
                runId=run_id,
                result={"status": "CANCELLED" if event_type == "RUN_CANCELLED" else "COMPLETED"},
                timestamp=timestamp,
            )
        if record["entity_type"] == "research_run" and event_type in self._RUN_FAILURE_EVENTS:
            return RunErrorEvent(message=f"Run terminated: {event_type}", code=event_type, timestamp=timestamp)
        if event_type in {"COLLABORATION_MESSAGE_RECORDED", "HUMAN_DIRECTIVE_RECORDED"}:
            return self._message_event(record, timestamp=timestamp)
        custom_name = self._CUSTOM_EVENTS.get(event_type)
        if custom_name is not None:
            return CustomEvent(name=custom_name, value=self._event_value(record), timestamp=timestamp)
        if event_type.startswith(self._ACTIVITY_PREFIXES):
            return ActivitySnapshotEvent(
                messageId=str(record["event_id"]),
                activityType=event_type,
                content=self._event_value(record),
                timestamp=timestamp,
            )
        return StateDeltaEvent(
            delta=[{"op": "add", "path": "/lastEvent", "value": self._event_value(record)}],
            timestamp=timestamp,
        )

    def _message_event(self, record: dict[str, Any], *, timestamp: int) -> AGUIEvent:
        message = self.api.collaboration_message(str(record["entity_id"]))
        if message["classification"] in {"LOCAL_ONLY", "SECRET"}:
            return CustomEvent(
                name="researchd.message.redacted",
                value={
                    "message_id": message["message_id"],
                    "classification": message["classification"],
                },
                timestamp=timestamp,
            )
        actor_type = message["sender_actor_type"]
        role: Literal["user", "assistant", "system", "tool"]
        role = "user" if actor_type == "human" else "assistant" if actor_type == "agent" else "system"
        return TextMessageChunkEvent(
            messageId=message["message_id"],
            role=role,
            delta=message["body"],
            timestamp=timestamp,
        )

    @staticmethod
    def _event_value(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "event_id": record["event_id"],
            "event_type": record["event_type"],
            "entity_type": record["entity_type"],
            "entity_id": record["entity_id"],
            "correlation_id": record["correlation_id"],
            "metadata": record["metadata"],
        }


__all__ = ["AGUIEvent", "AGUIProjectionAdapter", "ProjectedEvent"]
