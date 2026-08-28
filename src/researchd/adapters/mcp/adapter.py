"""MCP 2025-11-25 thin façades over native capability services."""

import json
from collections.abc import Callable, Iterable
from typing import Any, Protocol
from urllib.parse import urlparse


MCP_PROTOCOL_REVISION = "2025-11-25"


class NativeCapabilityService(Protocol):
    def run_target(self, target: str) -> dict[str, Any]: ...


class MCPProtocolError(ValueError):
    pass


class MCPStdioAdapter:
    """JSON-RPC line façade; handlers only dispatch to an injected native service."""

    adapter_version = "mcp-adapter-v1"
    protocol_revision = MCP_PROTOCOL_REVISION

    def __init__(self, service: NativeCapabilityService) -> None:
        self.service = service

    def handle_line(self, line: str) -> str:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise TypeError
            request_id = request.get("id")
            method = request.get("method")
            params = request.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise TypeError
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            return self._error(None, -32600, "invalid request")
        try:
            result: dict[str, Any]
            if method == "initialize":
                result = {"protocolVersion": MCP_PROTOCOL_REVISION, "capabilities": {"tools": {}}}
            elif method == "tools/list":
                result = {"tools": [{"name": "test.run", "description": "Run a registered test target", "inputSchema": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"], "additionalProperties": False}}]}
            elif method == "tools/call":
                if params.get("name") != "test.run":
                    raise MCPProtocolError("unknown tool")
                arguments = params.get("arguments", {})
                target = arguments.get("target") if isinstance(arguments, dict) else None
                if not isinstance(target, str) or not target or target.startswith("-"):
                    raise MCPProtocolError("target must be a nonempty safe string")
                result = {"content": [{"type": "json", "json": self.service.run_target(target)}], "isError": False}
            elif method == "notifications/initialized":
                return ""
            else:
                return self._error(request_id, -32601, "method not found")
            return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}, sort_keys=True, separators=(",", ":"))
        except MCPProtocolError as error:
            return self._error(request_id, -32602, str(error))

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> str:
        return json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}, sort_keys=True, separators=(",", ":"))


class MCPStreamableHTTPTestAdapter:
    """Minimal HTTP policy test façade; no listener is created by core."""

    adapter_version = "mcp-http-test-adapter-v1"
    protocol_revision = MCP_PROTOCOL_REVISION

    def __init__(self, stdio: MCPStdioAdapter, *, bind_host: str = "127.0.0.1", allowed_origin: str = "http://localhost") -> None:
        if bind_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("MCP HTTP test adapter must bind loopback in V1")
        self.stdio = stdio
        self.bind_host = bind_host
        self.allowed_origin = allowed_origin

    def handle(self, *, origin: str | None, body: str) -> tuple[int, str]:
        if origin != self.allowed_origin:
            return 403, json.dumps({"error": "invalid Origin"}, separators=(",", ":"))
        return 200, self.stdio.handle_line(body)
