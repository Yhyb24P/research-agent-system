"""Invocation-bound entry point for non-authoritative Agent-origin actions."""

from datetime import UTC, datetime
import hashlib

from pydantic import Field
from sqlalchemy.orm import Session, sessionmaker

from researchd.collaboration.contracts import CollaborationMessage
from researchd.collaboration.messages import CollaborationMessageService
from researchd.domain.base import DomainModel
from researchd.domain.enums import CollaborationPurpose, DataClassification, InvocationStatus
from researchd.domain.ids import AgentId, DelegationId, InvocationId, MessageId
from researchd.storage.models import AgentInvocationRecord, AgentRecord, AgentRuntimeRecord, CollaborationMessageRecord, DelegationRecord


class AgentMessageAction(DomainModel):
    action_id: str = Field(min_length=1, max_length=128)
    recipient_agent_id: AgentId | None = None
    purpose: CollaborationPurpose
    body: str = Field(min_length=1, max_length=32_768)
    classification: DataClassification = DataClassification.PROJECT_PRIVATE
    reply_to_message_id: MessageId | None = None


class AgentActionBroker:
    """Derive Agent identity and authoritative scope from one live Invocation."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def submit_message(
        self,
        invocation_id: InvocationId,
        action: AgentMessageAction,
    ) -> CollaborationMessage:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            invocation, delegation = self._require_authority(
                session, str(invocation_id), now,
            )
            if action.recipient_agent_id is not None:
                recipient = session.get(AgentRecord, str(action.recipient_agent_id))
                if recipient is None or not recipient.enabled:
                    raise ValueError("message recipient Agent is unavailable")
            digest = hashlib.sha256(
                f"{invocation.invocation_id}:{action.action_id}".encode()
            ).hexdigest()[:32]
            message_id = MessageId(f"msg_agent_{digest}")
            existing = session.get(CollaborationMessageRecord, str(message_id))
            if existing is not None:
                if (
                    existing.invocation_id != invocation.invocation_id
                    or existing.sender_actor_id != invocation.agent_id
                    or existing.recipient_agent_id != (
                        str(action.recipient_agent_id) if action.recipient_agent_id else None
                    )
                    or existing.purpose != action.purpose.value
                    or existing.body != action.body
                    or existing.classification != action.classification.value
                    or existing.reply_to_message_id != (
                        str(action.reply_to_message_id) if action.reply_to_message_id else None
                    )
                ):
                    raise ValueError("Agent action identity was reused with different content")
                return self._stored_message(existing)
            message = CollaborationMessage(
                message_id=message_id,
                run_id=invocation.run_id,
                work_order_id=invocation.work_order_id,
                delegation_id=DelegationId(delegation.delegation_id),
                invocation_id=InvocationId(invocation.invocation_id),
                reply_to_message_id=action.reply_to_message_id,
                sender_actor_type="agent",
                sender_actor_id=invocation.agent_id,
                recipient_agent_id=action.recipient_agent_id,
                purpose=action.purpose,
                body=action.body,
                classification=action.classification,
            )
            CollaborationMessageService.append_in_session(session, message, now=now)
            return message

    @staticmethod
    def _stored_message(row: CollaborationMessageRecord) -> CollaborationMessage:
        return CollaborationMessage(
            message_id=MessageId(row.message_id), run_id=row.run_id,
            work_order_id=row.work_order_id,
            delegation_id=DelegationId(row.delegation_id) if row.delegation_id else None,
            invocation_id=InvocationId(row.invocation_id) if row.invocation_id else None,
            reply_to_message_id=MessageId(row.reply_to_message_id) if row.reply_to_message_id else None,
            sender_actor_type=row.sender_actor_type, sender_actor_id=row.sender_actor_id,
            recipient_agent_id=AgentId(row.recipient_agent_id) if row.recipient_agent_id else None,
            purpose=CollaborationPurpose(row.purpose), body=row.body,
            classification=DataClassification(row.classification),
            metadata=dict(row.metadata_json),
        )

    @staticmethod
    def _require_authority(
        session: Session,
        invocation_id: str,
        now: datetime,
    ) -> tuple[AgentInvocationRecord, DelegationRecord]:
        invocation = session.get(AgentInvocationRecord, invocation_id)
        if invocation is None or invocation.status != InvocationStatus.RUNNING.value:
            raise ValueError("Agent action requires a running invocation")
        delegation = session.get(DelegationRecord, invocation.delegation_id)
        if (
            delegation is None
            or delegation.state != "RUNNING"
            or delegation.assigned_agent_id != invocation.agent_id
            or delegation.assigned_runtime_id != invocation.runtime_id
        ):
            raise ValueError("invocation no longer owns its delegation")
        runtime = session.get(AgentRuntimeRecord, invocation.runtime_id)
        agent = session.get(AgentRecord, invocation.agent_id)
        if (
            agent is None
            or not agent.enabled
            or runtime is None
            or not runtime.enabled
            or runtime.runtime_lease_id != invocation.runtime_lease_id
            or runtime.lease_expires_at is None
            or runtime.lease_expires_at <= now
        ):
            raise ValueError("invocation runtime lease is stale")
        return invocation, delegation


__all__ = ["AgentActionBroker", "AgentMessageAction"]
