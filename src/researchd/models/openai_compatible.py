import json
from urllib.parse import urlparse

import httpx

from researchd.models.cloud import CloudModelRequest, CloudModelResponse, CloudProviderUnavailable, CloudUsage


class OpenAICompatibleCloudModel:
    """Direct outbound HTTPS adapter; no SDK tools, sessions, tracing, or hosted execution."""

    sdk_tracing_enabled = False

    def __init__(
        self, *, base_url: str, api_key: str, model: str,
        allowed_hosts: frozenset[str], provider_name: str = "openai-compatible",
        timeout_seconds: float = 60, max_transport_response_bytes: int = 1_000_000,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "https" or parsed.hostname is None or parsed.hostname not in allowed_hosts
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or not api_key
        ):
            raise ValueError("cloud provider URL must be HTTPS and match the configured host allowlist")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_name = model
        self.provider_name = provider_name
        self.timeout_seconds = timeout_seconds
        self.max_transport_response_bytes = max_transport_response_bytes
        self.client = client

    async def complete(self, request: CloudModelRequest) -> CloudModelResponse:
        user_content = request.context_json
        if request.repair_instruction is not None:
            user_content += "\n\nSCHEMA_REPAIR_INSTRUCTION:\n" + request.repair_instruction
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": user_content},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": request.response_type, "strict": True, "schema": request.response_schema},
            },
            "max_tokens": request.max_output_tokens,
            "temperature": 0,
            "stream": False,
            "store": False,
        }
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False)
        try:
            async with client.stream(
                "POST", f"{self.base_url}/v1/chat/completions", json=payload,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            ) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_transport_response_bytes:
                        raise CloudProviderUnavailable("cloud provider response exceeds transport byte limit")
                    chunks.append(chunk)
                body = json.loads(b"".join(chunks))
                request_id_header = response.headers.get("x-request-id")
            text = body["choices"][0]["message"]["content"]
            usage = body.get("usage", {})
            if not isinstance(text, str):
                raise ValueError("provider response content must be text")
            return CloudModelResponse(
                text=text,
                usage=CloudUsage(
                    prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    completion_tokens=int(usage.get("completion_tokens", 0)),
                    total_tokens=int(usage.get("total_tokens", 0)),
                ),
                provider_request_id=request_id_header or body.get("id"),
            )
        except CloudProviderUnavailable:
            raise
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            retryable = status == 408 or status == 409 or status == 425 or status == 429 or 500 <= status <= 599
            retry_after: float | None = None
            value = error.response.headers.get("retry-after")
            if value is not None:
                try:
                    retry_after = max(0.0, float(value))
                except ValueError:
                    retry_after = None
            raise CloudProviderUnavailable(
                f"cloud provider HTTP {status}", retryable=retryable, retry_after_seconds=retry_after,
            ) from error
        except httpx.TimeoutException as error:
            raise CloudProviderUnavailable("cloud provider timeout", retryable=True) from error
        except httpx.HTTPError as error:
            raise CloudProviderUnavailable(f"cloud provider call failed: {type(error).__name__}", retryable=True) from error
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise CloudProviderUnavailable(f"cloud provider response invalid: {type(error).__name__}") from error
        finally:
            if owns_client:
                await client.aclose()
