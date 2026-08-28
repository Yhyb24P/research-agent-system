from datetime import UTC, datetime
from sqlalchemy.orm import Session, sessionmaker
from researchd.collaboration.contracts import AgentInvocationRequest, AgentInvocationResult
from researchd.domain.enums import InvocationStatus
from researchd.storage.models import AgentInvocationRecord, DelegationRecord

class InvocationService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def start(self, request: AgentInvocationRequest) -> None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            delegation = session.get(DelegationRecord, str(request.delegation_id))
            if delegation is None or delegation.assigned_agent_id != str(request.agent_id) or delegation.assigned_runtime_id != str(request.runtime_id):
                raise ValueError("invocation does not match assigned delegation")
            session.add(AgentInvocationRecord(invocation_id=str(request.invocation_id), delegation_id=str(request.delegation_id), run_id=request.run_id, work_order_id=request.work_order_id, attempt_id=request.attempt_id, agent_id=str(request.agent_id), runtime_id=str(request.runtime_id), purpose=request.purpose.value, status=InvocationStatus.RUNNING.value, input_sha256=request.input_sha256, created_at=now))

    def complete(self, result: AgentInvocationResult) -> None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.get(AgentInvocationRecord, str(result.invocation_id))
            if row is None or row.status != InvocationStatus.RUNNING.value:
                raise ValueError("invocation is not running")
            row.status, row.output_type, row.output_json = result.status.value, result.output_type, result.output
            row.reason_code, row.completed_at = result.reason_code, now
