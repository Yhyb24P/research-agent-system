from dataclasses import dataclass
from decimal import Decimal
from typing import Generic, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session, sessionmaker

from researchd.agents.prompts import CLOUD_LEAD_SYSTEM_PROMPT
from researchd.agents.schemas import EvidenceRequest, PlanProposal, WorkOrderProposal
from researchd.context.cloud_bundle import CloudContextBundle
from researchd.context.builder import CloudContextSelection, ContextBuilder
from researchd.context.serializer import cloud_bundle_sha256, serialize_cloud_bundle
from researchd.domain.review import ReviewDecision
from researchd.models.cloud import (
    CloudBudgetExceeded,
    CloudCallBudget,
    CloudCostMetadata,
    CloudModel,
    CloudModelRequest,
    CloudPricing,
    CloudProviderUnavailable,
    CloudSchemaInvalid,
)
from researchd.storage.models import AgentInteractionRecord, AuditEventRecord
from researchd.storage.repositories import utc_now

OutputT = TypeVar("OutputT", bound=BaseModel)


@dataclass(frozen=True)
class CloudLeadResult(Generic[OutputT]):
    output: OutputT
    interaction_id: str
    cost: CloudCostMetadata


class CloudLeadAdapter:
    """Stateless Cloud Lead facade: each call receives one reconstructed safe bundle."""

    def __init__(
        self, model: CloudModel, sessions: sessionmaker[Session], context_builder: ContextBuilder, *,
        budget: CloudCallBudget, pricing: CloudPricing,
    ) -> None:
        self.model = model
        self.sessions = sessions
        self.context_builder = context_builder
        self.budget = budget
        self.pricing = pricing

    async def propose_plan(self, selection: CloudContextSelection) -> CloudLeadResult[PlanProposal]:
        return await self._invoke(self._build(selection), PlanProposal, "PLAN")

    async def propose_work_order(self, selection: CloudContextSelection) -> CloudLeadResult[WorkOrderProposal]:
        return await self._invoke(self._build(selection), WorkOrderProposal, "WORK_ORDER")

    async def request_evidence(self, selection: CloudContextSelection) -> CloudLeadResult[EvidenceRequest]:
        return await self._invoke(self._build(selection), EvidenceRequest, "EVIDENCE_REQUEST")

    async def review(self, selection: CloudContextSelection) -> CloudLeadResult[ReviewDecision]:
        return await self._invoke(self._build(selection), ReviewDecision, "REVIEW")

    def _build(self, selection: CloudContextSelection) -> CloudContextBundle:
        if not isinstance(selection, CloudContextSelection):
            raise TypeError("Cloud Lead accepts only authoritative CloudContextSelection IDs")
        return self.context_builder.build_selection(selection)

    async def _invoke(
        self, bundle: CloudContextBundle, output_type: type[OutputT], purpose: str,
    ) -> CloudLeadResult[OutputT]:
        serialized = serialize_cloud_bundle(bundle)
        if len(serialized) > self.budget.max_input_bytes:
            raise CloudBudgetExceeded("cloud context exceeds input byte budget")
        interaction_id = f"interaction_{uuid4().hex}"
        created = utc_now()
        with self.sessions.begin() as session:
            session.add(AgentInteractionRecord(
                interaction_id=interaction_id, run_id=bundle.run_id,
                work_order_id=bundle.work_order_id, role="cloud_lead", purpose=purpose,
                provider=self.model.provider_name, model=self.model.model_name,
                bundle_sha256=cloud_bundle_sha256(bundle), response_type=output_type.__name__,
                response_json=None, status="IN_PROGRESS", reason_code=None, attempts=0,
                prompt_tokens=0, completion_tokens=0, total_tokens=0, cost_usd="0",
                provider_request_id=None, created_at=created, completed_at=None,
            ))
            session.add(AuditEventRecord(
                event_id=f"evt_{uuid4().hex}", event_type="CLOUD_INTERACTION_STARTED",
                run_id=bundle.run_id, entity_type="agent_interaction", entity_id=interaction_id,
                actor_type="controller", actor_id="cloud-lead-adapter", timestamp=created,
                correlation_id=bundle.work_order_id or bundle.run_id, causation_id=None,
                metadata_json={"purpose": purpose, "bundle_sha256": cloud_bundle_sha256(bundle)},
            ))
        attempts = 0
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        provider_request_id: str | None = None
        repair: str | None = None
        while attempts < self.budget.max_requests:
            attempts += 1
            request = CloudModelRequest(
                system_prompt=CLOUD_LEAD_SYSTEM_PROMPT,
                context_json=serialized.decode(), response_schema=output_type.model_json_schema(),
                response_type=output_type.__name__, repair_instruction=repair,
                max_output_tokens=self.budget.max_output_tokens,
            )
            try:
                response = await self.model.complete(request)
            except (CloudProviderUnavailable, TimeoutError) as error:
                self._finish(
                    interaction_id, status="WAITING_EXTERNAL", reason_code="CLOUD_UNAVAILABLE",
                    attempts=attempts, prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens, total_tokens=total_tokens,
                    cost=self._cost(prompt_tokens, completion_tokens), provider_request_id=provider_request_id,
                )
                raise CloudProviderUnavailable(str(error)) from error
            provider_request_id = response.provider_request_id or provider_request_id
            prompt_tokens += response.usage.prompt_tokens
            completion_tokens += response.usage.completion_tokens
            total_tokens += max(
                response.usage.total_tokens,
                response.usage.prompt_tokens + response.usage.completion_tokens,
            )
            cost = self._cost(prompt_tokens, completion_tokens)
            if total_tokens > self.budget.max_total_tokens:
                self._finish(
                    interaction_id, status="FAILED", reason_code="CLOUD_BUDGET_EXCEEDED",
                    attempts=attempts, prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens, total_tokens=total_tokens,
                    cost=cost, provider_request_id=provider_request_id,
                )
                raise CloudBudgetExceeded("cloud provider token usage exceeded budget")
            if len(response.text.encode()) > self.budget.max_response_bytes:
                validation_message = "provider response exceeds byte budget"
            else:
                try:
                    output = output_type.model_validate_json(response.text)
                except ValidationError as error:
                    validation_message = error.json(include_input=False, include_context=False)
                else:
                    self._finish(
                        interaction_id, status="COMPLETED", reason_code=None,
                        attempts=attempts, prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens, total_tokens=total_tokens,
                        cost=cost, provider_request_id=provider_request_id,
                        response_json=output.model_dump(mode="json"),
                    )
                    return CloudLeadResult(
                        output=output, interaction_id=interaction_id,
                        cost=CloudCostMetadata(
                            attempts=attempts, prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens, total_tokens=total_tokens,
                            cost_usd=cost,
                        ),
                    )
            repair = (
                "Your prior response failed schema validation. Return a complete replacement JSON object only. "
                f"Validation errors: {validation_message[:8192]}"
            )
        self._finish(
            interaction_id, status="FAILED", reason_code="CLOUD_SCHEMA_INVALID",
            attempts=attempts, prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens, total_tokens=total_tokens,
            cost=self._cost(prompt_tokens, completion_tokens), provider_request_id=provider_request_id,
        )
        raise CloudSchemaInvalid(f"cloud output invalid after {attempts} attempts")

    def _cost(self, prompt_tokens: int, completion_tokens: int) -> Decimal:
        million = Decimal(1_000_000)
        return (
            Decimal(prompt_tokens) * self.pricing.prompt_usd_per_million
            + Decimal(completion_tokens) * self.pricing.completion_usd_per_million
        ) / million

    def _finish(
        self, interaction_id: str, *, status: str, reason_code: str | None,
        attempts: int, prompt_tokens: int, completion_tokens: int,
        total_tokens: int, cost: Decimal, provider_request_id: str | None,
        response_json: dict[str, object] | None = None,
    ) -> None:
        with self.sessions.begin() as session:
            record = session.get(AgentInteractionRecord, interaction_id)
            if record is None:
                raise RuntimeError("cloud interaction reservation disappeared")
            record.status = status
            record.reason_code = reason_code
            record.attempts = attempts
            record.prompt_tokens = prompt_tokens
            record.completion_tokens = completion_tokens
            record.total_tokens = total_tokens
            record.cost_usd = format(cost, "f")
            record.provider_request_id = provider_request_id
            record.response_json = response_json
            record.completed_at = utc_now()
            session.add(AuditEventRecord(
                event_id=f"evt_{uuid4().hex}", event_type="CLOUD_INTERACTION_FINISHED",
                run_id=record.run_id, entity_type="agent_interaction", entity_id=interaction_id,
                actor_type="controller", actor_id="cloud-lead-adapter",
                timestamp=record.completed_at, correlation_id=record.work_order_id or record.run_id,
                causation_id=None, metadata_json={"status": status, "reason_code": reason_code, "attempts": attempts},
            ))
