from decimal import Decimal
import hashlib
import json
from typing import Any, Protocol
from urllib.parse import urlparse

from pydantic import Field, model_validator

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
    def __init__(self, message: str, *, retryable: bool = False, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


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
    max_cost_usd: Decimal = Field(default=Decimal("10"), gt=0)


class CloudProviderConfiguration(DomainModel):
    """Non-secret, qualification-relevant provider configuration snapshot."""

    configuration_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1, max_length=512)
    account_id: str = Field(min_length=1, max_length=256)
    project_id: str | None = Field(default=None, max_length=256)
    region: str | None = Field(default=None, max_length=128)
    model: str = Field(min_length=1, max_length=128)
    sdk_client: str = Field(min_length=1, max_length=128)
    request_timeout_seconds: float = Field(gt=0, le=3600)
    retry_max_requests: int = Field(gt=0, le=10)
    retry_backoff_seconds: float = Field(ge=0, le=300)
    retry_max_backoff_seconds: float = Field(ge=0, le=300)
    retention_policy: str = Field(min_length=1, max_length=256)
    training_opt_out: bool
    privacy_mode: str = Field(min_length=1, max_length=128)
    structured_output_mode: str = Field(min_length=1, max_length=128)
    token_accounting_source: str = Field(min_length=1, max_length=128)
    cost_accounting_source: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_egress_configuration(self) -> "CloudProviderConfiguration":
        parsed = urlparse(self.endpoint)
        if (
            parsed.scheme != "https" or parsed.hostname is None
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
        ):
            raise ValueError("provider endpoint must be a credential-free HTTPS origin or path")
        if self.retry_max_backoff_seconds < self.retry_backoff_seconds:
            raise ValueError("retry max backoff must be at least the initial backoff")
        return self

    def snapshot_sha256(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json", exclude_none=False), sort_keys=True,
            separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode()
        return hashlib.sha256(payload).hexdigest()


class CloudPricing(DomainModel):
    prompt_usd_per_million: Decimal = Field(ge=0)
    completion_usd_per_million: Decimal = Field(ge=0)


class CloudCostMetadata(DomainModel):
    attempts: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: Decimal
