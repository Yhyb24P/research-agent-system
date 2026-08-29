from datetime import UTC, datetime
from sqlalchemy.orm import Session, sessionmaker
from researchd.collaboration.contracts import AgentInvocationRequest, AgentInvocationResult, ExecuteInvocationInput
from researchd.domain.enums import InvocationStatus
from researchd.storage.models import AgentInvocationRecord, DelegationRecord, WorkspaceGrantRecord

class InvocationService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def start(self, request: AgentInvocationRequest) -> None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            delegation = session.get(DelegationRecord, str(request.delegation_id))
            if delegation is None or delegation.assigned_agent_id != str(request.agent_id) or delegation.assigned_runtime_id != str(request.runtime_id):
                raise ValueError("invocation does not match assigned delegation")
            if delegation.run_id != request.run_id or delegation.work_order_id != request.work_order_id:
                raise ValueError("invocation scope does not match delegation")
            if delegation.state not in {"ASSIGNED", "RUNNING"}:
                raise ValueError("delegation is terminal")
            binding_id = (
                request.typed_input.work_order.workspace_grant.workspace_grant_id
                if isinstance(request.typed_input, ExecuteInvocationInput)
                and request.typed_input.work_order.workspace_grant is not None
                else None
            )
            if request.workspace_grant_id != binding_id:
                raise ValueError("invocation workspace binding does not match its typed input")
            if request.workspace_grant_id is not None:
                grant = session.get(WorkspaceGrantRecord, request.workspace_grant_id)
                if (
                    grant is None
                    or grant.delegation_id != str(request.delegation_id)
                    or grant.state != "ACTIVE"
                    or grant.lease_expires_at is None
                    or grant.lease_expires_at <= now
                ):
                    raise ValueError("invocation workspace grant is missing, expired, or out of scope")
            session.add(AgentInvocationRecord(invocation_id=str(request.invocation_id), delegation_id=str(request.delegation_id), run_id=request.run_id, work_order_id=request.work_order_id, attempt_id=request.attempt_id, workspace_grant_id=request.workspace_grant_id, agent_id=str(request.agent_id), runtime_id=str(request.runtime_id), purpose=request.purpose.value, status=InvocationStatus.RUNNING.value, input_sha256=request.input_sha256, context_bundle_sha256=request.context_bundle.bundle_sha256 if request.context_bundle else None, context_bundle_json=request.context_bundle.model_dump(mode="json") if request.context_bundle else None, created_at=now))
            delegation.state = "RUNNING"
            delegation.updated_at = now
            delegation.version += 1

    def complete(self, result: AgentInvocationResult) -> None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.get(AgentInvocationRecord, str(result.invocation_id))
            if row is None or row.status != InvocationStatus.RUNNING.value:
                raise ValueError("invocation is not running")
            row.status, row.output_type, row.output_json = result.status.value, result.output_type, result.output
            row.reason_code, row.completed_at = result.reason_code, now
            delegation = session.get(DelegationRecord, row.delegation_id)
            if delegation is not None:
                delegation.state = "COMPLETED" if result.status is InvocationStatus.SUCCEEDED else result.status.value
                delegation.updated_at = now
                delegation.completed_at = now
                delegation.version += 1

    def recover_run(self, run_id: str) -> tuple[str, ...]:
        """Fail-closed invocations left RUNNING across a controller restart."""
        now = datetime.now(UTC)
        recovered: list[str] = []
        with self.sessions.begin() as session:
            rows = session.query(AgentInvocationRecord).filter(
                AgentInvocationRecord.run_id == run_id,
                AgentInvocationRecord.status == InvocationStatus.RUNNING.value,
            ).all()
            for row in rows:
                row.status = InvocationStatus.FAILED.value
                row.reason_code = "CONTROLLER_RESTARTED"
                row.completed_at = now
                delegation = session.get(DelegationRecord, row.delegation_id)
                if delegation is not None and delegation.state == "RUNNING":
                    delegation.state = InvocationStatus.FAILED.value
                    delegation.completed_at = now
                    delegation.updated_at = now
                    delegation.version += 1
                recovered.append(row.invocation_id)
        return tuple(recovered)
