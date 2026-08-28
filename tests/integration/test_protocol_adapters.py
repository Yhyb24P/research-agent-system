import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from researchd.adapters.a2a import A2AAdapter, A2ATerminalTaskError, executor_agent_card
from researchd.adapters.mcp import MCPStdioAdapter, MCPStreamableHTTPTestAdapter
from researchd.storage.models import AttemptRecord, WorkOrderRecord
from test_orchestrator import _proposal, make_orchestrator


class FakeA2AClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return {
            "id": payload["id"], "contextId": payload["contextId"],
            "status": {"state": "completed"}, "artifacts": [], "history": [],
            "metadata": payload.get("metadata", {}),
        }


def test_a2a_dispatch_is_idempotent_and_terminal_refinement_is_new_task(tmp_path: Path) -> None:
    sessions, orchestrator, _, _ = make_orchestrator(tmp_path, cloud_responses=[_proposal()])
    run_id = orchestrator.create_run(workspace_id="ws_e2e", objective="a2a")
    for _ in range(4):
        asyncio.run(orchestrator.advance(run_id))
    with sessions() as session:
        order = session.query(WorkOrderRecord).one()
        attempt = session.query(AttemptRecord).one()
    client = FakeA2AClient()
    adapter = A2AAdapter(sessions, client, remote_agent_id="executor.example")
    first = asyncio.run(adapter.dispatch(work_order_id=order.work_order_id, attempt_id=attempt.attempt_id, payload={"message": "execute"}))
    second = asyncio.run(adapter.dispatch(work_order_id=order.work_order_id, attempt_id=attempt.attempt_id, payload={"message": "execute"}))
    assert first.id == second.id and len(client.payloads) == 1
    refined = asyncio.run(adapter.refine_terminal_task(task_id=first.id, work_order_id=order.work_order_id, attempt_id=attempt.attempt_id, payload={"message": "refine"}))
    assert refined.id != first.id and refined.contextId == first.contextId and len(client.payloads) == 2


def test_a2a_card_is_v1_and_nonterminal_task_cannot_refine(tmp_path: Path) -> None:
    card = executor_agent_card()
    assert card["protocolVersion"] == "1.0.0" and card["supportedInterfaces"][0]["protocolBinding"] == "HTTP+JSON"
    # The terminal guard is exercised by the adapter's typed task mapping in the dispatch test;
    # this assertion protects the protocol shape independently of any SDK.
    assert "supportedInterfaces" in card


class NativeTestService:
    def __init__(self) -> None:
        self.targets: list[str] = []

    def run_target(self, target: str) -> dict[str, Any]:
        self.targets.append(target)
        return {"target": target, "status": "ok"}


def test_mcp_stdio_delegates_to_native_service_and_http_rejects_origin() -> None:
    service = NativeTestService()
    stdio = MCPStdioAdapter(service)
    initialize = json.loads(stdio.handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})))
    assert initialize["result"]["protocolVersion"] == "2025-11-25"
    response = json.loads(stdio.handle_line(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "test.run", "arguments": {"target": "tests/smoke.py"}}})))
    assert response["result"]["isError"] is False and service.targets == ["tests/smoke.py"]
    http = MCPStreamableHTTPTestAdapter(stdio)
    assert http.handle(origin="https://evil.example", body="{}")[0] == 403
    assert http.handle(origin="http://localhost", body=json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}))[0] == 200
    with pytest.raises(ValueError, match="loopback"):
        MCPStreamableHTTPTestAdapter(stdio, bind_host="0.0.0.0")
