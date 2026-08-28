import re
import math
from typing import Any
from collections.abc import Sequence


class DeterministicRedactor:
    def __init__(self, *, secret_literals: Sequence[str] = (), filesystem_prefixes: Sequence[str] = ()) -> None:
        self.secret_literals = tuple(sorted((value for value in secret_literals if value), key=len, reverse=True))
        self.filesystem_prefixes = tuple(sorted((value for value in filesystem_prefixes if value), key=len, reverse=True))
        self.patterns = (
            re.compile(r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----", re.DOTALL),
            re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
            re.compile(r"(?im)\b(?:AWS_SECRET_ACCESS_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|HF_TOKEN|GITHUB_TOKEN)\s*=\s*[^\s]+"),
        )

    def redact(self, text: str) -> str:
        result = text
        for pattern in self.patterns:
            result = pattern.sub("[REDACTED]", result)
        for literal in self.secret_literals:
            result = result.replace(literal, "[REDACTED]")
        for prefix in self.filesystem_prefixes:
            result = result.replace(prefix, "[REDACTED_PATH]")
        return result

    def redact_json(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if value is None or isinstance(value, bool) or isinstance(value, int):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("cloud-bound JSON cannot contain non-finite numbers")
            return value
        if isinstance(value, list):
            return [self.redact_json(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact_json(item) for item in value)
        if isinstance(value, dict):
            return {self.redact(str(key)): self.redact_json(item) for key, item in value.items()}
        raise ValueError("cloud-bound value is not deterministic JSON")
