import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from google.protobuf.json_format import ParseDict
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    ListTasksRequest,
    ListTasksResponse,
    StreamResponse,
    Task,
)

from researchd.adapters.a2a import (
    A2AAdapter,
    A2AMessage,
    A2APart,
    A2ATerminalTaskError,
    EXECUTOR_RESULT_MEDIA_TYPE,
    GRANTED_WORK_ORDER_MEDIA_TYPE,
    OfficialA2AClient,
    agent_card,
    encode_granted_work_order,
)
from researchd.adapters.mcp import MCPStdioAdapter, MCPStreamableHTTPTestAdapter
from researchd.collaboration.contracts import AgentInvocationRequest, ExecuteInvocationInput
from researchd.collaboration.contracts import AgentProfile, AgentRuntime, Delegation
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.gateway import CollaborationGateway
from researchd.collaboration.invocation import InvocationService
from researchd.collaboration.heterogeneous import A2ARemoteAgentAdapter
from researchd.collaboration.registry import AgentRegistryService
from researchd.collaboration.runtime import AgentAdapterCatalog
from researchd.domain.enums import AgentAdapterKind, AgentTrustZone, Capability, DelegationPurpose, InvocationStatus
from researchd.domain.ids import AgentId, AgentRuntimeId, DelegationId, InvocationId
from researchd.executor.contracts import GrantedWorkOrder, SandboxSpec
from researchd.storage.models import AgentInvocationRecord, AttemptRecord, WorkOrderRecord
from test_orchestrator import _proposal, make_orchestrator


class FakeA2AClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.cancelled_task_ids: list[str] = []

    async def send(
        self,
        payload: dict[str, Any],
        *,
        on_task: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        self.payloads.append(payload)
        message = payload["message"]
        work_order = message["parts"][0]["data"]
        attempt_id = work_order["attempt_id"]
        response = {
            "id": f"task_{message['messageId']}", "contextId": message["contextId"],
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{
                "artifactId": f"result_{attempt_id}",
                "parts": [{
                    "data": {
                        "attempt_id": attempt_id,
                        "status": "execution_complete",
                        "capability_results": [],
                        "reported_claims": ["remote ok"],
                        "errors": [],
                    },
                    "mediaType": EXECUTOR_RESULT_MEDIA_TYPE,
                }],
            }],
            "history": [message], "metadata": payload.get("metadata", {}),
        }
        if on_task is not None:
            on_task(response)
        return response

    async def cancel(self, *, task_id: str, tenant: str | None = None) -> dict[str, Any]:
        del tenant
        self.cancelled_task_ids.append(task_id)
        return {
            "id": task_id,
            "contextId": self.payloads[-1]["message"]["contextId"],
            "status": {"state": "TASK_STATE_CANCELED"},
            "artifacts": [],
            "history": [],
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
    granted = GrantedWorkOrder(
        attempt_id=attempt.attempt_id,
        objective=order.objective,
        granted_capabilities=frozenset(),
        sandbox=SandboxSpec(attempt_id=attempt.attempt_id, workspace="/workspace"),
    )
    first = asyncio.run(adapter.dispatch(
        work_order_id=order.work_order_id,
        attempt_id=attempt.attempt_id,
        message=encode_granted_work_order(granted),
    ))
    second = asyncio.run(adapter.dispatch(
        work_order_id=order.work_order_id,
        attempt_id=attempt.attempt_id,
        message=encode_granted_work_order(granted),
    ))
    assert first.id == second.id and len(client.payloads) == 1
    refined = asyncio.run(adapter.refine_terminal_task(
        task_id=first.id,
        work_order_id=order.work_order_id,
        attempt_id=attempt.attempt_id,
        message=encode_granted_work_order(granted),
    ))
    assert refined.id != first.id and refined.contextId == first.contextId and len(client.payloads) == 2
    assert first.id in client.payloads[1]["message"]["referenceTaskIds"]


def test_a2a_card_is_v1_and_nonterminal_task_cannot_refine(tmp_path: Path) -> None:
    profile = AgentProfile(
        agent_id=AgentId("agent_card"), display_name="Science Agent",
        roles=("reviewer",), skills=("evidence.inspect",), trust_zone=AgentTrustZone.REMOTE_PRIVATE,
    )
    runtime = AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_card"), agent_id=profile.agent_id,
        adapter_kind=AgentAdapterKind.A2A, runtime_name="A2A runtime",
        endpoint_ref="http://127.0.0.1:8787/a2a",
        metadata={"a2a_tenant": "tenant-science"},
    )
    card = agent_card(
        profile,
        runtime,
        security_schemes={
            "bearer": {
                "httpAuthSecurityScheme": {"scheme": "Bearer", "bearerFormat": "JWT"}
            }
        },
        security_requirements=({"schemes": {"bearer": {"list": []}}},),
    )
    assert "protocolVersion" not in card and "url" not in card
    assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert card["supportedInterfaces"][0]["protocolBinding"] == "HTTP+JSON"
    assert card["supportedInterfaces"][0]["tenant"] == "tenant-science"
    assert card["name"] == "Science Agent" and card["skills"][0]["tags"] == ["evidence.inspect"]
    assert "bearer" in card["securitySchemes"] and card["securityRequirements"]
    ParseDict(card, AgentCard())


def test_a2a_remote_agent_adapter_maps_typed_execute_to_invocation(tmp_path: Path) -> None:
    sessions, orchestrator, _, _ = make_orchestrator(tmp_path, cloud_responses=[_proposal()])
    run_id = orchestrator.create_run(workspace_id="ws_e2e", objective="a2a typed execute")
    for _ in range(4):
        asyncio.run(orchestrator.advance(run_id))
    with sessions() as session:
        order = session.query(WorkOrderRecord).one()
        attempt = session.query(AttemptRecord).one()
    registry = AgentRegistryService(sessions)
    registry.register_profile(AgentProfile(agent_id=AgentId("agent_remote"), display_name="Remote", roles=("executor",), trust_zone=AgentTrustZone.REMOTE_PRIVATE))
    registry.register_runtime(AgentRuntime(runtime_id=AgentRuntimeId("runtime_a2a"), agent_id=AgentId("agent_remote"), adapter_kind=AgentAdapterKind.A2A, runtime_name="A2A"))
    registry.acquire_runtime("runtime_a2a", owner_id="a2a-typed-test")
    delegation = DelegationService(sessions)
    delegation.create(Delegation(delegation_id=DelegationId("del_a2a_typed"), run_id=run_id, work_order_id=order.work_order_id, purpose=DelegationPurpose.EXECUTE, idempotency_key="del-a2a-typed"))
    delegation.assign("del_a2a_typed", agent_id="agent_remote", runtime_id="runtime_a2a")
    client = FakeA2AClient()
    delegate = A2AAdapter(sessions, client, remote_agent_id="agent.remote")
    adapter = A2ARemoteAgentAdapter(delegate)
    request = AgentInvocationRequest(
        invocation_id=InvocationId("inv_a2a_typed"), delegation_id=DelegationId("del_a2a_typed"),
        run_id=run_id, work_order_id=order.work_order_id, attempt_id=attempt.attempt_id,
        agent_id=AgentId("agent_remote"), runtime_id=AgentRuntimeId("runtime_a2a"),
        purpose=DelegationPurpose.EXECUTE, input_sha256="3" * 64,
        typed_input=ExecuteInvocationInput(work_order=GrantedWorkOrder(
            attempt_id=attempt.attempt_id, objective=order.objective,
            granted_capabilities=frozenset({Capability.TEST_RUN}),
            sandbox=SandboxSpec(attempt_id=attempt.attempt_id, workspace="/workspace"),
        )),
    )
    InvocationService(sessions).start(request)
    result = asyncio.run(adapter.invoke(request))
    assert result.status is InvocationStatus.SUCCEEDED and result.output_type == "ExecutorResult"
    assert result.output is not None and result.output["attempt_id"] == attempt.attempt_id
    assert len(client.payloads) == 1
    part = client.payloads[0]["message"]["parts"][0]
    assert part["mediaType"] == GRANTED_WORK_ORDER_MEDIA_TYPE
    assert part["data"]["attempt_id"] == attempt.attempt_id
    catalog = AgentAdapterCatalog(sessions)
    catalog.register(AgentAdapterKind.A2A, adapter)
    gateway = CollaborationGateway(
        delegations=delegation,
        invocations=InvocationService(sessions),
        catalog=catalog,
    )
    asyncio.run(gateway.cancel(attempt.attempt_id))
    assert client.cancelled_task_ids
    with sessions() as session:
        invocation = session.get(AgentInvocationRecord, str(request.invocation_id))
        assert invocation is not None and invocation.status == InvocationStatus.CANCELLED.value


def test_a2a_executor_result_crosses_collaboration_gateway(tmp_path: Path) -> None:
    sessions, orchestrator, _, _ = make_orchestrator(tmp_path, cloud_responses=[_proposal()])
    run_id = orchestrator.create_run(workspace_id="ws_e2e", objective="a2a gateway execute")
    for _ in range(4):
        asyncio.run(orchestrator.advance(run_id))
    with sessions() as session:
        order = session.query(WorkOrderRecord).one()
    registry = AgentRegistryService(sessions)
    registry.register_profile(AgentProfile(
        agent_id=AgentId("agent_a2a_gateway"),
        display_name="A2A executor",
        roles=("executor",),
        trust_zone=AgentTrustZone.REMOTE_PRIVATE,
    ))
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_a2a_gateway"),
        agent_id=AgentId("agent_a2a_gateway"),
        adapter_kind=AgentAdapterKind.A2A,
        runtime_name="A2A gateway runtime",
        endpoint_ref="http://127.0.0.1:8787/a2a",
    ))
    registry.acquire_runtime("runtime_a2a_gateway", owner_id="a2a-gateway-test")
    delegations = DelegationService(sessions)
    delegations.create(Delegation(
        delegation_id=DelegationId("del_a2a_gateway"),
        run_id=run_id,
        work_order_id=order.work_order_id,
        purpose=DelegationPurpose.EXECUTE,
        idempotency_key="del-a2a-gateway",
    ))
    delegations.assign(
        "del_a2a_gateway", agent_id="agent_a2a_gateway", runtime_id="runtime_a2a_gateway"
    )
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    attempt = AttemptRecord(
        attempt_id="att_a2a_gateway",
        work_order_id=order.work_order_id,
        delegation_id="del_a2a_gateway",
        state="RUNNING",
        terminal_at=None,
        version=1,
        created_at=now,
        updated_at=now,
    )
    with sessions.begin() as session:
        session.add(attempt)
    client = FakeA2AClient()
    catalog = AgentAdapterCatalog(sessions)
    catalog.register(
        AgentAdapterKind.A2A,
        A2ARemoteAgentAdapter(A2AAdapter(sessions, client, remote_agent_id="agent.a2a.gateway")),
    )
    gateway = CollaborationGateway(
        delegations=delegations,
        invocations=InvocationService(sessions),
        catalog=catalog,
    )
    result = asyncio.run(gateway.execute(order, attempt))
    assert result.attempt_id == attempt.attempt_id
    assert result.status == "execution_complete"
    assert result.reported_claims == ("remote ok",)


def test_official_sdk_client_aggregates_streamed_status_and_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    initial = StreamResponse()
    ParseDict({
        "task": {
            "id": "task_stream",
            "contextId": "ctx_stream",
            "status": {"state": "TASK_STATE_WORKING"},
        }
    }, initial)
    artifact_update = StreamResponse()
    ParseDict({
        "artifactUpdate": {
            "taskId": "task_stream",
            "contextId": "ctx_stream",
            "artifact": {
                "artifactId": "result_stream",
                "parts": [{
                    "data": {
                        "attempt_id": "att_stream",
                        "status": "execution_complete",
                        "capability_results": [],
                        "reported_claims": ["stream ok"],
                        "errors": [],
                    },
                    "mediaType": EXECUTOR_RESULT_MEDIA_TYPE,
                }],
            },
            "lastChunk": True,
        }
    }, artifact_update)
    status_update = StreamResponse()
    ParseDict({
        "statusUpdate": {
            "taskId": "task_stream",
            "contextId": "ctx_stream",
            "status": {"state": "TASK_STATE_COMPLETED"},
        }
    }, status_update)

    class StreamingClient:
        closed = False
        tenants: list[str] = []

        async def send_message(self, request: object) -> Any:
            del request
            for response in (initial, artifact_update, status_update):
                yield response

        async def close(self) -> None:
            self.closed = True

        async def cancel_task(self, request: CancelTaskRequest) -> Task:
            self.tenants.append(request.tenant)
            task = Task()
            ParseDict({
                "id": request.id,
                "contextId": "ctx_stream",
                "status": {"state": "TASK_STATE_CANCELED"},
            }, task)
            return task

        async def list_tasks(self, request: ListTasksRequest) -> ListTasksResponse:
            self.tenants.append(request.tenant)
            response = ListTasksResponse()
            ParseDict({
                "tasks": [{
                    "id": "task_stream",
                    "contextId": request.context_id,
                    "status": {"state": "TASK_STATE_COMPLETED"},
                }],
                "pageSize": request.page_size,
                "totalSize": 1,
            }, response)
            return response

    streaming_client = StreamingClient()

    async def create_client(endpoint: str) -> StreamingClient:
        assert endpoint == "http://agent.example"
        return streaming_client

    monkeypatch.setattr("a2a.client.create_client", create_client)
    message = A2AMessage(
        messageId="msg_stream",
        contextId="ctx_stream",
        role="ROLE_USER",
        parts=(A2APart(data={"attempt_id": "att_stream"}, mediaType=GRANTED_WORK_ORDER_MEDIA_TYPE),),
    )
    response = asyncio.run(OfficialA2AClient("http://agent.example").send({"message": message.model_dump(mode="json", exclude_none=True)}))
    assert response["status"]["state"] == "TASK_STATE_COMPLETED"
    assert response["artifacts"][0]["parts"][0]["mediaType"] == EXECUTOR_RESULT_MEDIA_TYPE
    cancelled = asyncio.run(OfficialA2AClient("http://agent.example").cancel(
        task_id="task_stream", tenant="tenant-stream"
    ))
    listed = asyncio.run(OfficialA2AClient("http://agent.example").list_tasks(
        tenant="tenant-stream", context_id="ctx_stream"
    ))
    assert cancelled["status"]["state"] == "TASK_STATE_CANCELED"
    assert listed["tasks"][0]["id"] == "task_stream"
    assert streaming_client.tenants == ["tenant-stream", "tenant-stream"]
    assert streaming_client.closed


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
