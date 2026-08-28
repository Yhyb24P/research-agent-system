from datetime import UTC, datetime
from researchd.collaboration.contracts import CollaborationMessage, HumanDirective
from researchd.storage.models import CollaborationMessageRecord
from sqlalchemy.orm import Session, sessionmaker


class CollaborationMessageService:
    """Append-only message storage; text never executes control-plane operations."""
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def append(self, message: CollaborationMessage) -> None:
        with self.sessions.begin() as session:
            session.add(CollaborationMessageRecord(message_id=str(message.message_id), run_id=message.run_id, sender_actor_type=message.sender_actor_type, sender_actor_id=message.sender_actor_id, recipient_agent_id=str(message.recipient_agent_id) if message.recipient_agent_id else None, purpose=message.purpose, body=message.body, metadata_json=dict(message.metadata), created_at=datetime.now(UTC)))

    def record_directive(self, directive: HumanDirective, *, run_id: str, sender_actor_id: str) -> CollaborationMessage:
        message = CollaborationMessage(message_id=directive.directive_id, run_id=run_id, sender_actor_type="human", sender_actor_id=sender_actor_id, purpose="DIRECTIVE", body=directive.text, metadata={"requested_action": directive.requested_action or ""})
        self.append(message)
        return message
