from typing import Protocol

from researchd.executor.contracts import LocalAgentRequest, LocalAgentResponse


class LocalModelUnavailable(RuntimeError):
    pass


class LocalModel(Protocol):
    async def complete(self, request: LocalAgentRequest) -> LocalAgentResponse: ...
