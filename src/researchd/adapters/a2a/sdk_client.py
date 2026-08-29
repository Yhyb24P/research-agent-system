"""Optional official A2A SDK client isolated from the trusted core."""

from collections.abc import Callable
from typing import Any

from researchd.adapters.a2a.adapter import A2AAdapterError


class OfficialA2AClient:
    """Use a2a-sdk for discovery, transport, streaming, tenancy, and cancellation."""

    def __init__(
        self,
        endpoint: str,
        *,
        event_observer: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        if not endpoint:
            raise ValueError("A2A endpoint is required")
        self.endpoint = endpoint
        self.event_observer = event_observer

    async def send(
        self,
        payload: dict[str, Any],
        *,
        on_task: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        from a2a.client import create_client
        from a2a.types import Message, SendMessageRequest, Task, TaskState
        from google.protobuf.json_format import MessageToDict, ParseDict

        request = SendMessageRequest()
        ParseDict(payload, request)
        client = await create_client(self.endpoint)
        task: Task | None = None
        try:
            async for response in client.send_message(request):
                if response.HasField("task"):
                    task = Task()
                    task.CopyFrom(response.task)
                    self._observe({
                        "kind": "task",
                        "task_id": task.id,
                        "context_id": task.context_id,
                        "state": TaskState.Name(task.status.state),
                    })
                    if on_task is not None:
                        on_task(MessageToDict(task, preserving_proto_field_name=False))
                elif response.HasField("status_update"):
                    if task is None:
                        raise A2AAdapterError("A2A stream emitted a status update before its Task")
                    status_update = response.status_update
                    self._require_stream_scope(
                        task.id, task.context_id, status_update.task_id, status_update.context_id
                    )
                    self._observe({
                        "kind": "status",
                        "task_id": status_update.task_id,
                        "context_id": status_update.context_id,
                        "state": TaskState.Name(status_update.status.state),
                    })
                    task.status.CopyFrom(status_update.status)
                elif response.HasField("artifact_update"):
                    if task is None:
                        raise A2AAdapterError("A2A stream emitted an artifact update before its Task")
                    artifact_update = response.artifact_update
                    self._require_stream_scope(
                        task.id, task.context_id, artifact_update.task_id, artifact_update.context_id
                    )
                    self._observe({
                        "kind": "artifact",
                        "task_id": artifact_update.task_id,
                        "context_id": artifact_update.context_id,
                        "artifact_id": artifact_update.artifact.artifact_id,
                    })
                    self._apply_artifact_update(task, artifact_update)
                elif response.HasField("message"):
                    message: Message = response.message
                    raise A2AAdapterError(f"A2A Agent returned Message {message.message_id!r}, not a Task")
        finally:
            await client.close()
        if task is None:
            raise A2AAdapterError("A2A response stream did not contain a Task")
        return MessageToDict(task, preserving_proto_field_name=False)

    def _observe(self, event: dict[str, str]) -> None:
        if self.event_observer is not None:
            self.event_observer(event)

    async def cancel(self, *, task_id: str, tenant: str | None = None) -> dict[str, Any]:
        from a2a.client import create_client
        from a2a.types import CancelTaskRequest
        from google.protobuf.json_format import MessageToDict

        client = await create_client(self.endpoint)
        try:
            task = await client.cancel_task(CancelTaskRequest(id=task_id, tenant=tenant or ""))
        finally:
            await client.close()
        return MessageToDict(task, preserving_proto_field_name=False)

    async def list_tasks(
        self,
        *,
        tenant: str | None = None,
        context_id: str | None = None,
        page_size: int = 100,
    ) -> dict[str, Any]:
        from a2a.client import create_client
        from a2a.types import ListTasksRequest
        from google.protobuf.json_format import MessageToDict

        client = await create_client(self.endpoint)
        try:
            response = await client.list_tasks(
                ListTasksRequest(tenant=tenant or "", context_id=context_id or "", page_size=page_size)
            )
        finally:
            await client.close()
        return MessageToDict(response, preserving_proto_field_name=False)

    async def get_task(
        self,
        *,
        task_id: str,
        tenant: str | None = None,
        history_length: int = 0,
    ) -> dict[str, Any]:
        from a2a.client import create_client
        from a2a.types import GetTaskRequest
        from google.protobuf.json_format import MessageToDict

        client = await create_client(self.endpoint)
        try:
            task = await client.get_task(GetTaskRequest(
                id=task_id,
                tenant=tenant or "",
                history_length=history_length,
            ))
        finally:
            await client.close()
        return MessageToDict(task, preserving_proto_field_name=False)

    @staticmethod
    def _require_stream_scope(task_id: str, context_id: str, update_task_id: str, update_context_id: str) -> None:
        if task_id != update_task_id or context_id != update_context_id:
            raise A2AAdapterError("A2A stream update changed Task or context scope")

    @staticmethod
    def _apply_artifact_update(task: Any, update: Any) -> None:
        existing = next((artifact for artifact in task.artifacts if artifact.artifact_id == update.artifact.artifact_id), None)
        if update.append:
            if existing is None:
                raise A2AAdapterError("A2A artifact append referenced an unknown artifact")
            existing.parts.extend(update.artifact.parts)
            existing.metadata.update(dict(update.artifact.metadata.items()))
            return
        if existing is None:
            task.artifacts.append(update.artifact)
        else:
            existing.CopyFrom(update.artifact)
