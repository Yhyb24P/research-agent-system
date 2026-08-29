"""Independent official-SDK A2A Agent used only by the IQ01 process boundary."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCard, Part, Task, TaskState, TaskStatus
from google.protobuf.json_format import MessageToDict, ParseDict
from starlette.applications import Starlette


GRANTED_WORK_ORDER_MEDIA_TYPE = "application/vnd.researchd.granted-work-order+json"
EXECUTOR_RESULT_MEDIA_TYPE = "application/vnd.researchd.executor-result+json"


class QualificationAgent(AgentExecutor):
    """A separate Agent implementation with scenario-controlled protocol output."""

    def __init__(self, trace_path: Path) -> None:
        self.trace_path = trace_path

    def _trace(self, value: dict[str, Any]) -> None:
        with self.trace_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.message is None or context.task_id is None or context.context_id is None:
            raise ValueError("typed A2A task context is required")
        message = MessageToDict(context.message)
        parts = message.get("parts")
        if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
            raise ValueError("exactly one input part is required")
        part = parts[0]
        if part.get("mediaType") != GRANTED_WORK_ORDER_MEDIA_TYPE:
            raise ValueError("unsupported input media type")
        work_order = part.get("data")
        if not isinstance(work_order, dict):
            raise ValueError("typed work order data is required")
        attempt_id = work_order.get("attempt_id")
        objective = work_order.get("objective")
        if not isinstance(attempt_id, str) or not isinstance(objective, str):
            raise ValueError("work order scope is malformed")

        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        self._trace({
            "event": "execute",
            "task_id": context.task_id,
            "context_id": context.context_id,
            "message_id": message.get("messageId"),
            "tenant": context.tenant,
            "attempt_id": attempt_id,
            "objective": objective,
        })
        await event_queue.enqueue_event(Task(
            id=context.task_id,
            context_id=context.context_id,
            status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            history=[context.message],
        ))
        await updater.update_status(
            TaskState.TASK_STATE_WORKING,
            metadata={"sequence": "working"},
        )
        if objective == "IQ01_AUTH_REQUIRED":
            await updater.requires_auth()
            return
        if objective == "IQ01_SLOW_CANCEL":
            await asyncio.sleep(60)
            return

        output_attempt = "att_wrong_scope" if objective == "IQ01_WRONG_ATTEMPT" else attempt_id
        result: dict[str, Any] = {
            "attempt_id": output_attempt,
            "status": "execution_complete",
            "capability_results": [],
            "reported_claims": ["independent Agent completed"],
            "errors": [],
        }
        if objective == "IQ01_MALFORMED_OUTPUT":
            result = {"attempt_id": attempt_id, "status": "not-a-valid-status"}
        artifact_part = Part()
        ParseDict({"data": result, "mediaType": EXECUTOR_RESULT_MEDIA_TYPE}, artifact_part)
        await updater.add_artifact(
            parts=[artifact_part],
            artifact_id=f"result_{context.task_id}",
            name="independent ExecutorResult",
            last_chunk=True,
        )
        if objective == "IQ01_DUPLICATE_OUTPUT":
            duplicate = Part()
            ParseDict({"data": result, "mediaType": EXECUTOR_RESULT_MEDIA_TYPE}, duplicate)
            await updater.add_artifact(
                parts=[duplicate],
                artifact_id=f"duplicate_{context.task_id}",
                name="duplicate ExecutorResult",
                last_chunk=True,
            )
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.task_id is None or context.context_id is None:
            raise ValueError("cancel scope is missing")
        self._trace({
            "event": "cancel",
            "task_id": context.task_id,
            "context_id": context.context_id,
            "tenant": context.tenant,
        })
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def build_app(*, host: str, port: int, tenant: str, trace_path: Path) -> Starlette:
    card = AgentCard()
    ParseDict({
        "name": "IQ01 Independent Qualification Agent",
        "description": "Official SDK server process for bounded interoperability qualification",
        "supportedInterfaces": [{
            "url": f"http://{host}:{port}/a2a",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
            "tenant": tenant,
        }],
        "version": "iq01-agent-1",
        "capabilities": {"streaming": True, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": [GRANTED_WORK_ORDER_MEDIA_TYPE],
        "defaultOutputModes": [EXECUTOR_RESULT_MEDIA_TYPE],
        "skills": [{
            "id": "executor.qualification",
            "name": "Qualification Executor",
            "description": "Returns typed bounded qualification results",
            "tags": ["qualification", "executor"],
            "inputModes": [GRANTED_WORK_ORDER_MEDIA_TYPE],
            "outputModes": [EXECUTOR_RESULT_MEDIA_TYPE],
        }],
    }, card)
    handler = DefaultRequestHandler(
        agent_executor=QualificationAgent(trace_path),
        task_store=InMemoryTaskStore(owner_resolver=lambda context: context.tenant),
        agent_card=card,
    )
    return Starlette(routes=[
        *create_agent_card_routes(card),
        *create_jsonrpc_routes(handler, rpc_url="/a2a"),
    ])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--trace", required=True, type=Path)
    args = parser.parse_args()
    args.trace.parent.mkdir(parents=True, exist_ok=True)
    uvicorn.run(
        build_app(host=args.host, port=args.port, tenant=args.tenant, trace_path=args.trace),
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
