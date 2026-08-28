from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from researchd.executor.capability_broker import CapabilityBroker
from researchd.executor.contracts import CapabilityResult, ExecutorResult, GrantedWorkOrder, LocalAgentRequest
from researchd.models.base import LocalModel, LocalModelUnavailable
from researchd.storage.models import ExecutorDispatchRecord


class DuplicateDispatchInProgress(RuntimeError):
    pass


class LocalExecutorWorker:
    def __init__(self, model: LocalModel, broker: CapabilityBroker, sessions: sessionmaker[Session]) -> None:
        self.model = model
        self.broker = broker
        self.sessions = sessions

    async def execute(self, work_order: GrantedWorkOrder) -> ExecutorResult:
        cached = self._reserve_or_reuse(work_order.attempt_id)
        if cached is not None:
            return cached
        results: list[CapabilityResult] = []
        claims: list[str] = []
        try:
            for _ in range(work_order.max_agent_steps):
                response = await self.model.complete(LocalAgentRequest(
                    objective=work_order.objective,
                    prior_results=tuple(results),
                    granted_capabilities=work_order.granted_capabilities,
                ))
                if response.final_claim is not None:
                    claims.append(response.final_claim)
                if not response.actions:
                    final = ExecutorResult(
                        attempt_id=work_order.attempt_id, status="execution_complete",
                        capability_results=tuple(results), reported_claims=tuple(claims), errors=(),
                    )
                    self._complete(final)
                    return final
                for action in response.actions:
                    if len(results) >= work_order.max_agent_steps:
                        break
                    results.append(self.broker.execute(
                        action, granted=work_order.granted_capabilities, sandbox=work_order.sandbox,
                    ))
            final = ExecutorResult(
                attempt_id=work_order.attempt_id, status="step_limit",
                capability_results=tuple(results), reported_claims=tuple(claims),
                errors=("local executor step limit reached",),
            )
        except LocalModelUnavailable as error:
            final = ExecutorResult(
                attempt_id=work_order.attempt_id, status="model_unavailable",
                capability_results=tuple(results), reported_claims=tuple(claims),
                errors=(str(error),),
            )
        self._complete(final)
        return final

    def _reserve_or_reuse(self, attempt_id: str) -> ExecutorResult | None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            record = session.get(ExecutorDispatchRecord, attempt_id)
            if record is not None:
                if record.status == "COMPLETED" and record.result_json is not None:
                    return ExecutorResult.model_validate(record.result_json)
                raise DuplicateDispatchInProgress(attempt_id)
            session.add(ExecutorDispatchRecord(
                attempt_id=attempt_id, status="RUNNING", result_json=None,
                created_at=now, updated_at=now,
            ))
        return None

    def _complete(self, result: ExecutorResult) -> None:
        with self.sessions.begin() as session:
            record = session.get(ExecutorDispatchRecord, result.attempt_id)
            if record is None:
                raise RuntimeError("executor dispatch reservation disappeared")
            record.status = "COMPLETED"
            record.result_json = result.model_dump(mode="json")
            record.updated_at = datetime.now(UTC)
