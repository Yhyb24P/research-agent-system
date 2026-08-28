from decimal import Decimal
from typing import Any, Protocol

from pydantic import Field

from researchd.domain.base import DomainModel


class CloudUsage(DomainModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class CloudModelRequest(DomainModel):
    system_prompt: str
    context_json: str
    response_schema: dict[str, Any]
    response_type: str
    repair_instruction: str | None = None
    max_output_tokens: int = Field(gt=0)


class CloudModelResponse(DomainModel):
    text: str
    usage: CloudUsage
    provider_request_id: str | None = None


class CloudModel(Protocol):
    provider_name: str
    model_name: str

    async def complete(self, request: CloudModelRequest) -> CloudModelResponse: ...


class CloudProviderUnavailable(RuntimeError):
    pass


class CloudSchemaInvalid(RuntimeError):
    pass


class CloudBudgetExceeded(RuntimeError):
    pass


class CloudCallBudget(DomainModel):
    max_requests: int = Field(default=3, gt=0, le=10)
    max_input_bytes: int = Field(default=512_000, gt=0)
    max_output_tokens: int = Field(default=4096, gt=0)
    max_response_bytes: int = Field(default=256_000, gt=0)
    max_total_tokens: int = Field(default=50_000, gt=0)


class CloudPricing(DomainModel):
    prompt_usd_per_million: Decimal = Field(ge=0)
    completion_usd_per_million: Decimal = Field(ge=0)


class CloudCostMetadata(DomainModel):
    attempts: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: Decimal
