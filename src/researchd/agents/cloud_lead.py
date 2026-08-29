import asyncio
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
    CloudProviderConfiguration,
    CloudProviderUnavailable,
    CloudSchemaInvalid,
)
from researchd.storage.models import AgentInteractionRecord, AuditEventRecord, CloudInteractionGovernanceRecord
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
        configuration: CloudProviderConfiguration, budget: CloudCallBudget, pricing: CloudPricing,
        retry_backoff_seconds: float = 0.25, max_retry_backoff_seconds: float = 8.0,
    ) -> None:
        self.model = model
        self.sessions = sessions
        self.context_builder = context_builder
        self.configuration = configuration
        self.budget = budget
        self.pricing = pricing
        if retry_backoff_seconds < 0 or max_retry_backoff_seconds < retry_backoff_seconds:
            raise ValueError("invalid cloud retry backoff bounds")
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_retry_backoff_seconds = max_retry_backoff_seconds
        self._validate_configuration()

    async def propose_plan(self, selection: CloudContextSelection) -> CloudLeadResult[PlanProposal]:
        return await self._invoke(self._build(selection), PlanProposal, "PLAN", selection.invocation_id)

    async def propose_work_order(self, selection: CloudContextSelection) -> CloudLeadResult[WorkOrderProposal]:
        return await self._invoke(self._build(selection), WorkOrderProposal, "WORK_ORDER", selection.invocation_id)

    async def request_evidence(self, selection: CloudContextSelection) -> CloudLeadResult[EvidenceRequest]:
        return await self._invoke(self._build(selection), EvidenceRequest, "EVIDENCE_REQUEST", selection.invocation_id)

    async def review(self, selection: CloudContextSelection) -> CloudLeadResult[ReviewDecision]:
        return await self._invoke(self._build(selection), ReviewDecision, "REVIEW", selection.invocation_id)

    def _validate_configuration(self) -> None:
        if self.configuration.provider != self.model.provider_name or self.configuration.model != self.model.model_name:
            raise ValueError("provider configuration does not match the selected provider/model")
        if self.configuration.retry_max_requests != self.budget.max_requests:
            raise ValueError("provider configuration retry limit does not match the call budget")
        if (
            self.configuration.retry_backoff_seconds != self.retry_backoff_seconds
            or self.configuration.retry_max_backoff_seconds != self.max_retry_backoff_seconds
        ):
            raise ValueError("provider configuration retry policy does not match the adapter")
        endpoint = getattr(self.model, "base_url", None)
        if endpoint is not None and str(endpoint).rstrip("/") != self.configuration.endpoint.rstrip("/"):
            raise ValueError("provider configuration endpoint does not match the adapter")
        timeout = getattr(self.model, "timeout_seconds", None)
        if timeout is not None and float(timeout) != self.configuration.request_timeout_seconds:
            raise ValueError("provider configuration timeout does not match the adapter")

    def _build(self, selection: CloudContextSelection) -> CloudContextBundle:
        if not isinstance(selection, CloudContextSelection):
            raise TypeError("Cloud Lead accepts only authoritative CloudContextSelection IDs")
        return self.context_builder.build_selection(selection)

    async def _invoke(
        self, bundle: CloudContextBundle, output_type: type[OutputT], purpose: str,
        invocation_id: str | None,
    ) -> CloudLeadResult[OutputT]:
        serialized = serialize_cloud_bundle(bundle)
        if len(serialized) > self.budget.max_input_bytes:
            raise CloudBudgetExceeded("cloud context exceeds input byte budget")
        interaction_id = f"interaction_{uuid4().hex}"
        created = utc_now()
        with self.sessions.begin() as session:
            session.add(AgentInteractionRecord(
                interaction_id=interaction_id, invocation_id=invocation_id, run_id=bundle.run_id,
                work_order_id=bundle.work_order_id, role="cloud_lead", purpose=purpose,
                provider=self.model.provider_name, model=self.model.model_name,
                bundle_sha256=cloud_bundle_sha256(bundle), response_type=output_type.__name__,
                response_json=None, status="IN_PROGRESS", reason_code=None, attempts=0,
                prompt_tokens=0, completion_tokens=0, total_tokens=0, cost_usd="0",
                provider_request_id=None, created_at=created, completed_at=None,
            ))
            session.add(CloudInteractionGovernanceRecord(
                interaction_id=interaction_id,
                provider_configuration_id=self.configuration.configuration_id,
                provider_configuration_sha256=self.configuration.snapshot_sha256(),
                provider_configuration_json=self.configuration.model_dump(mode="json"),
                created_at=created,
            ))
            session.add(AuditEventRecord(
                event_id=f"evt_{uuid4().hex}", event_type="CLOUD_INTERACTION_STARTED",
                run_id=bundle.run_id, entity_type="agent_interaction", entity_id=interaction_id,
                actor_type="controller", actor_id="cloud-lead-adapter", timestamp=created,
                correlation_id=bundle.work_order_id or bundle.run_id, causation_id=None,
                metadata_json={
                    "purpose": purpose, "bundle_sha256": cloud_bundle_sha256(bundle),
                    "provider_configuration_id": self.configuration.configuration_id,
                    "provider_configuration_sha256": self.configuration.snapshot_sha256(),
                },
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
            except asyncio.CancelledError:
                self._finish(
                    interaction_id, status="CANCELLED", reason_code="CLOUD_CANCELLED",
                    attempts=attempts, prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens, total_tokens=total_tokens,
                    cost=self._cost(prompt_tokens, completion_tokens), provider_request_id=provider_request_id,
                )
                raise
            except (CloudProviderUnavailable, TimeoutError) as error:
                retryable = isinstance(error, CloudProviderUnavailable) and error.retryable
                if retryable and attempts < self.budget.max_requests:
                    provider_delay = error.retry_after_seconds if isinstance(error, CloudProviderUnavailable) else None
                    delay = provider_delay if provider_delay is not None else min(
                        self.retry_backoff_seconds * (2 ** (attempts - 1)), self.max_retry_backoff_seconds,
                    )
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        self._finish(
                            interaction_id, status="CANCELLED", reason_code="CLOUD_CANCELLED",
                            attempts=attempts, prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens, total_tokens=total_tokens,
                            cost=self._cost(prompt_tokens, completion_tokens), provider_request_id=provider_request_id,
                        )
                        raise
                    continue
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
            if total_tokens > self.budget.max_total_tokens or cost > self.budget.max_cost_usd:
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
