from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import httpx
import pytest
from a2a.client import A2ACardResolver, create_client
from a2a.client.errors import A2AClientError
from a2a.types import AgentCard, ListTasksRequest
from alembic import command
from alembic.config import Config
from google.protobuf.json_format import MessageToDict
from sqlalchemy.orm import Session, sessionmaker

from researchd.adapters.a2a import A2AAdapter, OfficialA2AClient, encode_granted_work_order
from researchd.collaboration.contracts import (
    AgentInvocationRequest,
    AgentProfile,
    AgentRuntime,
    Delegation,
    ExecuteInvocationInput,
)
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.heterogeneous import A2ARemoteAgentAdapter
from researchd.collaboration.invocation import InvocationService
from researchd.collaboration.registry import AgentRegistryService
from researchd.domain.enums import (
    AgentAdapterKind,
    AgentTrustZone,
    DelegationPurpose,
    InvocationStatus,
    ResearchRunState,
    WorkOrderState,
)
from researchd.domain.ids import AgentId, AgentRuntimeId, DelegationId, InvocationId
from researchd.executor.contracts import GrantedWorkOrder, SandboxSpec
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    AgentInteractionRecord,
    AgentInvocationRecord,
    ApprovalGrantRecord,
    ApprovalRequestRecord,
    AttemptRecord,
    AuditEventRecord,
    ResearchRunRecord,
    WorkOrderRecord,
    WorkspaceRecord,
)


ROOT = Path(__file__).parents[2]
AGENT_SCRIPT = ROOT / "tests" / "qualification" / "independent_a2a_agent.py"
TENANT = "tenant-iq01"
AGENT_ID = AgentId("agent_iq01_independent")
RUNTIME_ID = AgentRuntimeId("runtime_iq01_independent")


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@dataclass
class IndependentServer:
    base_url: str
    trace_path: Path
    process: subprocess.Popen[bytes]
    log_stream: BinaryIO

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.log_stream.close()


def _start_server(root: Path, *, name: str, port: int | None = None) -> IndependentServer:
    root.mkdir(parents=True, exist_ok=True)
    port = port if port is not None else _available_port()
    trace_path = root / f"{name}-trace.jsonl"
    log_stream = (root / f"{name}-server.log").open("wb")
    process = subprocess.Popen(
        (
            sys.executable,
            str(AGENT_SCRIPT),
            "--port", str(port),
            "--tenant", TENANT,
            "--trace", str(trace_path),
        ),
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=log_stream,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log_stream.flush()
            raise RuntimeError((root / f"{name}-server.log").read_text())
        try:
            response = httpx.get(f"{base_url}/.well-known/agent-card.json", timeout=0.5)
            if response.status_code == 200:
                return IndependentServer(base_url, trace_path, process, log_stream)
        except httpx.RequestError:
            pass
        time.sleep(0.05)
    process.terminate()
    process.wait(timeout=5)
    log_stream.close()
    raise RuntimeError("independent A2A Agent did not become ready")


def _database(path: Path) -> sessionmaker[Session]:
    path.parent.mkdir(parents=True, exist_ok=True)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")
    return session_factory(create_sqlite_engine(path))


def _controller(path: Path, endpoint: str) -> sessionmaker[Session]:
    sessions = _database(path)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(WorkspaceRecord(
            workspace_id="ws_iq01", name="IQ01", version=1, created_at=now, updated_at=now
        ))
    registry = AgentRegistryService(sessions)
    registry.register_profile(AgentProfile(
        agent_id=AGENT_ID,
        display_name="Independent IQ01 Agent",
        roles=("executor",),
        skills=("executor.qualification",),
        trust_zone=AgentTrustZone.REMOTE_PRIVATE,
    ))
    registry.register_runtime(AgentRuntime(
        runtime_id=RUNTIME_ID,
        agent_id=AGENT_ID,
        adapter_kind=AgentAdapterKind.A2A,
        runtime_name="Independent official SDK server",
        endpoint_ref=endpoint,
        protocols=("A2A/1.0",),
        metadata={"a2a_tenant": TENANT, "implementation": "a2a-sdk-server"},
    ))
    registry.acquire_runtime(str(RUNTIME_ID), owner_id="iq01-controller")
    return sessions


def _request(
    sessions: sessionmaker[Session], *, scenario: str, ordinal: int
) -> AgentInvocationRequest:
    suffix = f"{ordinal:02d}_{scenario.lower()}"
    run_id = f"run_{suffix}"
    work_order_id = f"wo_{suffix}"
    attempt_id = f"att_{suffix}"
    delegation_id = DelegationId(f"del_{suffix}")
    invocation_id = InvocationId(f"inv_{suffix}")
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(ResearchRunRecord(
            run_id=run_id,
            workspace_id="ws_iq01",
            objective=scenario,
            state=ResearchRunState.ACTIVE.value,
            version=1,
            created_at=now,
            updated_at=now,
        ))
        session.flush()
        session.add(WorkOrderRecord(
            work_order_id=work_order_id,
            run_id=run_id,
            parent_work_order_id=None,
            objective=scenario,
            state=WorkOrderState.EXECUTING.value,
            idempotency_key=f"iq01-{suffix}",
            contract={},
            version=1,
            created_at=now,
            updated_at=now,
        ))
        session.flush()
        session.add(AttemptRecord(
            attempt_id=attempt_id,
            work_order_id=work_order_id,
            state="RUNNING",
            terminal_at=None,
            version=1,
            created_at=now,
            updated_at=now,
        ))
    delegations = DelegationService(sessions)
    delegations.create(Delegation(
        delegation_id=delegation_id,
        run_id=run_id,
        work_order_id=work_order_id,
        purpose=DelegationPurpose.EXECUTE,
        required_roles=("executor",),
        idempotency_key=f"iq01-delegation-{suffix}",
    ))
    delegations.assign(str(delegation_id), agent_id=str(AGENT_ID), runtime_id=str(RUNTIME_ID))
    request = AgentInvocationRequest(
        invocation_id=invocation_id,
        delegation_id=delegation_id,
        run_id=run_id,
        work_order_id=work_order_id,
        attempt_id=attempt_id,
        agent_id=AGENT_ID,
        runtime_id=RUNTIME_ID,
        purpose=DelegationPurpose.EXECUTE,
        input_sha256=hashlib.sha256(f"{suffix}:{scenario}".encode()).hexdigest(),
        endpoint_ref=None,
        typed_input=ExecuteInvocationInput(work_order=GrantedWorkOrder(
            attempt_id=attempt_id,
            objective=scenario,
            granted_capabilities=frozenset(),
            sandbox=SandboxSpec(attempt_id=attempt_id, workspace="/workspace"),
        )),
    )
    InvocationService(sessions).start(request)
    return request


async def _card(base_url: str) -> AgentCard:
    async with httpx.AsyncClient() as http:
        return await A2ACardResolver(http, base_url).get_agent_card()


async def _run_matrix(
    root: Path, *, endpoint: str, server: IndependentServer, cycle: int
) -> tuple[dict[str, object], dict[str, object]]:
    sessions = _controller(root / "controller.db", endpoint)
    stream_trace: list[dict[str, str]] = []
    client = OfficialA2AClient(endpoint, event_observer=stream_trace.append)
    delegate = A2AAdapter(sessions, client, remote_agent_id=str(AGENT_ID))
    remote = A2ARemoteAgentAdapter(delegate)

    card = await _card(endpoint)
    assert card.name == "IQ01 Independent Qualification Agent"
    assert card.version == "iq01-agent-1"
    assert len(card.supported_interfaces) == 1
    interface = card.supported_interfaces[0]
    assert interface.protocol_binding == "JSONRPC"
    assert interface.protocol_version == "1.0" and interface.tenant == TENANT

    success = _request(sessions, scenario="IQ01_SUCCESS", ordinal=1)
    success_result = await remote.invoke(success)
    InvocationService(sessions).complete(success_result)
    assert success_result.status is InvocationStatus.SUCCEEDED
    assert success_result.output_type == "ExecutorResult"
    assert success_result.output is not None
    assert success_result.output["attempt_id"] == success.attempt_id
    assert success.work_order_id is not None
    assert success.attempt_id is not None
    assert isinstance(success.typed_input, ExecuteInvocationInput)
    assert [item["kind"] for item in stream_trace] == ["task", "status", "artifact", "status"]
    scope = {(item["task_id"], item["context_id"]) for item in stream_trace}
    assert len(scope) == 1
    assert [item.get("state") for item in stream_trace] == [
        "TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", None, "TASK_STATE_COMPLETED"
    ]

    with sessions() as session:
        interaction = session.query(AgentInteractionRecord).filter_by(
            attempt_id=success.attempt_id
        ).one()
        task_id = interaction.a2a_task_id
        assert task_id is not None
        assert session.query(AttemptRecord).filter_by(attempt_id=success.attempt_id).count() == 1
        assert session.query(WorkOrderRecord).filter_by(work_order_id=success.work_order_id).count() == 1
    fetched = await client.get_task(task_id=task_id, tenant=TENANT, history_length=10)
    listed = await client.list_tasks(tenant=TENANT, context_id=f"ctx_{success.run_id}")
    assert fetched["id"] == task_id
    assert [item["id"] for item in listed["tasks"]] == [task_id]

    other_card = AgentCard()
    other_card.CopyFrom(card)
    other_card.supported_interfaces[0].tenant = "tenant-other"
    other_client = await create_client(other_card)
    try:
        isolated = await other_client.list_tasks(ListTasksRequest(page_size=100))
    finally:
        await other_client.close()
    assert isolated.total_size == 0 and not isolated.tasks

    trace_before_duplicate = server.trace_path.read_text().count('"event":"execute"')
    duplicate = await delegate.dispatch(
        work_order_id=str(success.work_order_id),
        attempt_id=str(success.attempt_id),
        message=encode_granted_work_order(success.typed_input.work_order),
        tenant=TENANT,
        invocation_id=str(success.invocation_id),
    )
    assert duplicate.id == task_id
    assert server.trace_path.read_text().count('"event":"execute"') == trace_before_duplicate

    negative_results: dict[str, str] = {}
    for ordinal, scenario in enumerate((
        "IQ01_AUTH_REQUIRED",
        "IQ01_WRONG_ATTEMPT",
        "IQ01_MALFORMED_OUTPUT",
        "IQ01_DUPLICATE_OUTPUT",
    ), start=2):
        request = _request(sessions, scenario=scenario, ordinal=ordinal)
        result = await remote.invoke(request)
        negative_results[scenario] = result.reason_code or ""
        if scenario == "IQ01_AUTH_REQUIRED":
            assert result.status is InvocationStatus.RUNNING
            assert result.reason_code == "A2A_TASK_AUTH_REQUIRED"
        else:
            assert result.status is InvocationStatus.FAILED
            assert result.reason_code == "A2A_EXECUTOR_RESULT_INVALID"
            InvocationService(sessions).complete(result)
    with sessions() as session:
        assert session.query(ApprovalRequestRecord).count() == 0
        assert session.query(ApprovalGrantRecord).count() == 0

    cancel_request = _request(sessions, scenario="IQ01_SLOW_CANCEL", ordinal=6)
    cancel_future = asyncio.create_task(remote.invoke(cancel_request))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with sessions() as session:
            bound = session.query(AgentInteractionRecord).filter_by(
                attempt_id=cancel_request.attempt_id
            ).one_or_none()
            if bound is not None and bound.a2a_task_id is not None:
                break
        await asyncio.sleep(0.02)
    else:
        cancel_future.cancel()
        raise AssertionError("streaming Task was not bound before cancellation")
    await remote.cancel(str(cancel_request.invocation_id))
    cancelled = await asyncio.wait_for(cancel_future, timeout=5)
    assert cancelled.status is InvocationStatus.CANCELLED
    InvocationService(sessions).complete(cancelled)
    with sessions() as session:
        row = session.get(AgentInvocationRecord, str(cancel_request.invocation_id))
        assert row is not None and row.status == InvocationStatus.CANCELLED.value

    disconnected_port = _available_port()
    disconnected_endpoint = f"http://127.0.0.1:{disconnected_port}"
    disconnected_sessions = _controller(root / "disconnect.db", disconnected_endpoint)
    disconnected_request = _request(
        disconnected_sessions, scenario="IQ01_SUCCESS", ordinal=7
    )
    reconnect_delegate = A2AAdapter(
        disconnected_sessions,
        OfficialA2AClient(disconnected_endpoint),
        remote_agent_id=str(AGENT_ID),
    )
    reconnect_remote = A2ARemoteAgentAdapter(reconnect_delegate)
    with pytest.raises((A2AClientError, httpx.RequestError)):
        await reconnect_remote.invoke(disconnected_request)
    reconnect_server = _start_server(
        root,
        name=f"reconnect-{cycle}",
        port=disconnected_port,
    )
    try:
        reconnected = await reconnect_remote.invoke(disconnected_request)
        assert reconnected.status is InvocationStatus.SUCCEEDED
    finally:
        reconnect_server.stop()
    with disconnected_sessions() as session:
        assert session.query(AttemptRecord).count() == 1
        assert session.query(AgentInteractionRecord).count() == 1
        reconnect_counts = {
            "attempts": session.query(AttemptRecord).count(),
            "interactions": session.query(AgentInteractionRecord).count(),
        }

    with sessions() as session:
        summary = {
            "success_status": success_result.status.value,
            "negative_results": negative_results,
            "cancel_status": cancelled.status.value,
            "attempt_count": session.query(AttemptRecord).count(),
            "interaction_count": session.query(AgentInteractionRecord).count(),
            "approval_count": session.query(ApprovalRequestRecord).count(),
            "stream_kinds": [item["kind"] for item in stream_trace[:4]],
        }
        success_attempt = session.get(AttemptRecord, success.attempt_id)
        success_interaction = session.query(AgentInteractionRecord).filter_by(
            attempt_id=success.attempt_id
        ).one()
        audit_trace = [
            {
                "audit_seq": event.audit_seq,
                "event_type": event.event_type,
                "run_id": event.run_id,
                "entity_id": event.entity_id,
            }
            for event in session.query(AuditEventRecord)
            .filter(AuditEventRecord.event_type.like("A2A_%"))
            .order_by(AuditEventRecord.audit_seq)
        ]
    detail = {
        "cycle": cycle,
        "agent_card": MessageToDict(card, preserving_proto_field_name=False),
        "implementation": {
            "server": "independent official-SDK process; imports no researchd modules",
            "a2a_sdk": importlib.metadata.version("a2a-sdk"),
            "server_script_sha256": hashlib.sha256(AGENT_SCRIPT.read_bytes()).hexdigest(),
        },
        "successful_mapping": {
            "run_id": success.run_id,
            "work_order_id": success.work_order_id,
            "attempt_id": success.attempt_id,
            "invocation_id": str(success.invocation_id),
            "a2a_task_id": success_interaction.a2a_task_id,
            "a2a_context_id": success_interaction.a2a_context_id,
            "attempt_state_after_protocol_reconciliation": (
                success_attempt.state if success_attempt is not None else None
            ),
            "executor_result_sha256": hashlib.sha256(json.dumps(
                success_result.output,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()).hexdigest(),
        },
        "stream_trace": stream_trace,
        "agent_trace": [
            json.loads(line)
            for line in server.trace_path.read_text(encoding="utf-8").splitlines()
        ],
        "audit_trace": audit_trace,
        "negative_results": negative_results,
        "reconnect_counts": reconnect_counts,
        "summary": summary,
    }
    return summary, detail


def test_iq01_matrix_passes_twice_from_clean_controller(tmp_path: Path) -> None:
    summaries: list[dict[str, object]] = []
    cycle_details: list[dict[str, object]] = []
    for cycle in (1, 2):
        root = tmp_path / f"cycle-{cycle}"
        server = _start_server(root, name="independent")
        try:
            summary, detail = asyncio.run(_run_matrix(
                root,
                endpoint=server.base_url,
                server=server,
                cycle=cycle,
            ))
            summaries.append(summary)
            cycle_details.append(detail)
        finally:
            server.stop()
    assert summaries[0] == summaries[1]
    report_path = os.environ.get("IQ01_REPORT")
    if report_path:
        destination = Path(report_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps({
            "gate_id": "IQ01",
            "checks": [f"IQ01-{number:02d}" for number in range(1, 13)],
            "clean_cycle_summaries_equal": True,
            "cycles": cycle_details,
        }, sort_keys=True, indent=2) + "\n", encoding="utf-8")
