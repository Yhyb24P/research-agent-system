from datetime import UTC, datetime
from uuid import uuid4

from researchd.collaboration.contracts import CollaborationMessage, HumanDirective
from researchd.domain.enums import CollaborationPurpose
from researchd.storage.models import (
    AgentInvocationRecord,
    AuditEventRecord,
    CollaborationMessageRecord,
    DelegationRecord,
    ResearchRunRecord,
    WorkOrderRecord,
)
from sqlalchemy.orm import Session, sessionmaker


class CollaborationMessageService:
    """Append-only message storage; text never executes control-plane operations."""
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def append(self, message: CollaborationMessage) -> None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            self._validate_scope(session, message)
            session.add(CollaborationMessageRecord(message_id=str(message.message_id), run_id=message.run_id, work_order_id=message.work_order_id, delegation_id=str(message.delegation_id) if message.delegation_id else None, invocation_id=str(message.invocation_id) if message.invocation_id else None, reply_to_message_id=str(message.reply_to_message_id) if message.reply_to_message_id else None, sender_actor_type=message.sender_actor_type, sender_actor_id=message.sender_actor_id, recipient_agent_id=str(message.recipient_agent_id) if message.recipient_agent_id else None, purpose=message.purpose.value, body=message.body, classification=message.classification.value, metadata_json=dict(message.metadata), created_at=now))
            session.add(AuditEventRecord(
                event_id=f"evt_{uuid4().hex}",
                event_type="HUMAN_DIRECTIVE_RECORDED" if message.purpose is CollaborationPurpose.DIRECTIVE else "COLLABORATION_MESSAGE_RECORDED",
                run_id=message.run_id,
                entity_type="collaboration_message",
                entity_id=str(message.message_id),
                actor_type=message.sender_actor_type,
                actor_id=message.sender_actor_id,
                timestamp=now,
                correlation_id=message.work_order_id or message.run_id,
                causation_id=None,
                metadata_json={
                    "purpose": message.purpose.value,
                    "classification": message.classification.value,
                },
            ))

    def record_directive(self, directive: HumanDirective, *, run_id: str, sender_actor_id: str, work_order_id: str | None = None) -> CollaborationMessage:
        message = CollaborationMessage(message_id=directive.directive_id, run_id=run_id, work_order_id=work_order_id, sender_actor_type="human", sender_actor_id=sender_actor_id, purpose=CollaborationPurpose.DIRECTIVE, body=directive.text, metadata={"requested_action": directive.requested_action or ""})
        self.append(message)
        return message

    @staticmethod
    def _validate_scope(session: Session, message: CollaborationMessage) -> None:
        if session.get(ResearchRunRecord, message.run_id) is None:
            raise ValueError("message run does not exist")
        references = (
            (WorkOrderRecord, message.work_order_id),
            (DelegationRecord, str(message.delegation_id) if message.delegation_id else None),
            (AgentInvocationRecord, str(message.invocation_id) if message.invocation_id else None),
            (CollaborationMessageRecord, str(message.reply_to_message_id) if message.reply_to_message_id else None),
        )
        for record_type, identifier in references:
            if identifier is None:
                continue
            record = session.get(record_type, identifier)
            if record is None or record.run_id != message.run_id:
                raise ValueError("message reference is outside its run")
