"""A2A 1.0 boundary adapter; internal controller records stay authoritative."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.adapters.a2a.schemas import (
    A2AAgentCard,
    A2AAgentSkill,
    A2ACapabilities,
    A2AInterface,
    A2AMessage,
    A2ASendMessageRequest,
    A2ATask,
    A2A_PROTOCOL_VERSION,
)
from researchd.storage.models import AgentInteractionRecord, AuditEventRecord, AttemptRecord, WorkOrderRecord
from researchd.storage.repositories import utc_now

if TYPE_CHECKING:
    from researchd.collaboration.contracts import AgentProfile, AgentRuntime


class A2AClient(Protocol):
    async def send(
        self,
        payload: dict[str, Any],
        *,
        on_task: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]: ...

    async def cancel(self, *, task_id: str, tenant: str | None = None) -> dict[str, Any]: ...


class A2AAdapterError(RuntimeError):
    pass


class A2ATerminalTaskError(A2AAdapterError):
    pass


def agent_card(
    profile: AgentProfile,
    runtime: AgentRuntime,
    *,
    url: str | None = None,
    security_schemes: dict[str, dict[str, Any]] | None = None,
    security_requirements: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Project a trusted Agent identity into A2A discovery metadata."""
    endpoint = url or runtime.endpoint_ref
    if endpoint is None:
        raise ValueError("A2A Agent Card requires a runtime endpoint")
    return A2AAgentCard(
        name=profile.display_name,
        description=f"Agent roles: {', '.join(profile.roles) if profile.roles else 'unspecified'}",
        supportedInterfaces=(A2AInterface(
            url=endpoint,
            protocolBinding="HTTP+JSON",
            protocolVersion=A2A_PROTOCOL_VERSION,
            tenant=runtime.metadata.get("a2a_tenant"),
        ),),
        capabilities=A2ACapabilities(streaming=True, pushNotifications=False, extendedAgentCard=False),
        securitySchemes=security_schemes or {},
        securityRequirements=security_requirements,
        skills=tuple(A2AAgentSkill(
            id=skill,
            name=skill,
            description=f"Trusted profile skill {skill}",
            tags=(skill,),
        ) for skill in profile.skills),
    ).model_dump(mode="json", exclude_none=True)


class A2AAdapter:
    adapter_version = "a2a-adapter-v1"
    supported_protocol_revision = A2A_PROTOCOL_VERSION
    tested_sdk_version = "a2a-sdk>=1.0,<2"

    def __init__(self, sessions: sessionmaker[Session], client: A2AClient, *, remote_agent_id: str) -> None:
        self.sessions = sessions
        self.client = client
        self.remote_agent_id = remote_agent_id

    async def dispatch(
        self,
        *,
        work_order_id: str,
        attempt_id: str,
        message: A2AMessage,
        tenant: str | None = None,
        force_new: bool = False,
        invocation_id: str | None = None,
    ) -> A2ATask:
        order, attempt = self._scope(work_order_id, attempt_id)
        context_id = f"ctx_{order.run_id}"
        content = message.model_dump(mode="json", exclude_none=True)
        key = self._key(work_order_id, attempt_id, self.remote_agent_id, content if force_new else None)
        existing = None if force_new else self._interaction_for_attempt(work_order_id, attempt_id)
        if existing and existing.status == "COMPLETED" and existing.response_json:
            return A2ATask.model_validate(existing.response_json)
        outbound = message.model_copy(update={
            "messageId": key,
            "contextId": message.contextId or context_id,
            "metadata": {
                **message.metadata,
                "internalWorkOrderId": work_order_id,
                "internalAttemptId": attempt_id,
                "idempotencyKey": key,
            },
        })
        body = A2ASendMessageRequest(tenant=tenant, message=outbound).model_dump(
            mode="json", exclude_none=True
        )
        interaction_id = existing.interaction_id if existing else f"interaction_{uuid4().hex}"
        if existing is None:
            self._reserve(interaction_id, order, attempt, outbound.contextId or context_id, body, invocation_id=invocation_id)
        response = await self.client.send(
            body,
            on_task=lambda task: self._bind_task(
                interaction_id,
                task,
                expected_context_id=outbound.contextId or context_id,
            ),
        )
        task = A2ATask.model_validate(response)
        if task.contextId != outbound.contextId:
            raise A2AAdapterError("A2A response changed the invocation context")
        self._finish(interaction_id, task)
        return task

    async def refine_terminal_task(
        self,
        *,
        task_id: str,
        work_order_id: str,
        attempt_id: str,
        message: A2AMessage,
        tenant: str | None = None,
    ) -> A2ATask:
        with self.sessions() as session:
            interaction = session.scalar(select(AgentInteractionRecord).where(AgentInteractionRecord.a2a_task_id == task_id))
            if interaction is None or interaction.work_order_id != work_order_id or interaction.attempt_id != attempt_id:
                raise A2AAdapterError("A2A task mapping is missing or internally out of scope")
            if not interaction.response_json:
                raise A2AAdapterError("cannot refine a task without a terminal response")
            task = A2ATask.model_validate(interaction.response_json)
            if task.status.state not in {
                "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"
            }:
                raise A2ATerminalTaskError("only a terminal A2A task can be refined")
            context_id = interaction.a2a_context_id
        refined = message.model_copy(update={
            "contextId": context_id,
            "referenceTaskIds": tuple(dict.fromkeys((*message.referenceTaskIds, task_id))),
        })
        return await self.dispatch(
            work_order_id=work_order_id,
            attempt_id=attempt_id,
            message=refined,
            tenant=tenant,
            force_new=True,
        )

    async def cancel(self, invocation_id: str, *, tenant: str | None = None) -> A2ATask:
        with self.sessions() as session:
            interaction = session.scalar(select(AgentInteractionRecord).where(
                AgentInteractionRecord.invocation_id == invocation_id,
                AgentInteractionRecord.purpose == "A2A_DISPATCH",
            ).order_by(AgentInteractionRecord.created_at.desc()).limit(1))
            if interaction is None or interaction.a2a_task_id is None:
                raise A2AAdapterError("A2A invocation has no cancellable Task mapping")
            interaction_id = interaction.interaction_id
            task_id = interaction.a2a_task_id
        task = A2ATask.model_validate(await self.client.cancel(task_id=task_id, tenant=tenant))
        if task.id != task_id:
            raise A2AAdapterError("A2A cancellation returned a different Task")
        self._finish(interaction_id, task)
        return task

    def _scope(self, work_order_id: str, attempt_id: str) -> tuple[WorkOrderRecord, AttemptRecord]:
        with self.sessions() as session:
            order = session.get(WorkOrderRecord, work_order_id)
            attempt = session.get(AttemptRecord, attempt_id)
            if order is None or attempt is None or attempt.work_order_id != work_order_id:
                raise A2AAdapterError("WorkOrder/Attempt mapping is missing or mismatched")
            session.expunge(order)
            session.expunge(attempt)
            return order, attempt

    def _interaction_for_attempt(self, work_order_id: str, attempt_id: str) -> AgentInteractionRecord | None:
        with self.sessions() as session:
            return session.scalar(select(AgentInteractionRecord).where(
                AgentInteractionRecord.work_order_id == work_order_id,
                AgentInteractionRecord.attempt_id == attempt_id,
                AgentInteractionRecord.remote_agent_id == self.remote_agent_id,
                AgentInteractionRecord.purpose == "A2A_DISPATCH",
            ).order_by(AgentInteractionRecord.created_at.desc()).limit(1))

    def _reserve(self, interaction_id: str, order: WorkOrderRecord, attempt: AttemptRecord, context_id: str, body: dict[str, Any], *, invocation_id: str | None = None) -> None:
        now = utc_now()
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.sessions.begin() as session:
            session.add(AgentInteractionRecord(
                interaction_id=interaction_id, invocation_id=invocation_id, run_id=order.run_id, work_order_id=order.work_order_id,
                attempt_id=attempt.attempt_id, remote_agent_id=self.remote_agent_id,
                a2a_context_id=context_id, a2a_task_id=None, role="a2a_adapter", purpose="A2A_DISPATCH",
                provider=self.remote_agent_id, model="a2a-1.0", bundle_sha256=digest,
                response_type="A2ATask", response_json=None, status="IN_PROGRESS", reason_code=None,
                attempts=1, prompt_tokens=0, completion_tokens=0, total_tokens=0, cost_usd="0",
                provider_request_id=None, created_at=now, completed_at=None,
            ))
            session.add(AuditEventRecord(
                event_id=f"evt_{uuid4().hex}", event_type="A2A_TASK_DISPATCHED", run_id=order.run_id,
                entity_type="agent_interaction", entity_id=interaction_id, actor_type="controller",
                actor_id="a2a-adapter", timestamp=now, correlation_id=attempt.attempt_id,
                causation_id=None, metadata_json={"message_id": body["message"]["messageId"], "context_id": context_id, "remote_agent_id": self.remote_agent_id},
            ))

    def _finish(self, interaction_id: str, task: A2ATask) -> None:
        with self.sessions.begin() as session:
            record = session.get(AgentInteractionRecord, interaction_id)
            if record is None:
                raise A2AAdapterError("A2A interaction reservation disappeared")
            record.a2a_task_id = task.id
            record.response_json = task.model_dump(mode="json")
            record.status = "COMPLETED" if task.status.state in {
                "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"
            } else "IN_PROGRESS"
            record.reason_code = None if record.status == "COMPLETED" else "A2A_TASK_NONTERMINAL"
            record.completed_at = utc_now() if record.status == "COMPLETED" else None
            session.add(AuditEventRecord(
                event_id=f"evt_{uuid4().hex}", event_type="A2A_TASK_RECONCILED", run_id=record.run_id,
                entity_type="agent_interaction", entity_id=interaction_id, actor_type="controller",
                actor_id="a2a-adapter", timestamp=utc_now(), correlation_id=record.attempt_id or record.run_id,
                causation_id=None, metadata_json={"task_id": task.id, "state": task.status.state},
            ))

    def _bind_task(
        self,
        interaction_id: str,
        task_value: dict[str, Any],
        *,
        expected_context_id: str,
    ) -> None:
        task = A2ATask.model_validate(task_value)
        if task.contextId != expected_context_id:
            raise A2AAdapterError("A2A initial Task changed the invocation context")
        with self.sessions.begin() as session:
            record = session.get(AgentInteractionRecord, interaction_id)
            if record is None or record.status != "IN_PROGRESS":
                raise A2AAdapterError("A2A interaction cannot bind a running Task")
            if record.a2a_task_id not in {None, task.id}:
                raise A2AAdapterError("A2A interaction changed its running Task identity")
            record.a2a_task_id = task.id
            session.add(AuditEventRecord(
                event_id=f"evt_{uuid4().hex}",
                event_type="A2A_TASK_BOUND",
                run_id=record.run_id,
                entity_type="agent_interaction",
                entity_id=interaction_id,
                actor_type="controller",
                actor_id="a2a-adapter",
                timestamp=utc_now(),
                correlation_id=record.attempt_id or record.run_id,
                causation_id=None,
                metadata_json={"task_id": task.id, "context_id": task.contextId},
            ))

    @staticmethod
    def _key(work_order_id: str, attempt_id: str, remote_agent_id: str, payload: dict[str, Any] | None = None) -> str:
        suffix = "" if payload is None else json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "a2a_" + hashlib.sha256(f"{remote_agent_id}:{work_order_id}:{attempt_id}:{suffix}".encode()).hexdigest()
