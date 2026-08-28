# MCP Mapping

The MCP façade is pinned to revision `2025-11-25`. `MCPStdioAdapter` implements the small JSON-RPC surface needed for `initialize`, `tools/list`, and `tools/call`; its handler only validates wire parameters and calls an injected native capability service. Business policy, sandboxing, output limits, and artifact registration remain in the native service/controller.

The optional HTTP test façade models Streamable HTTP policy without creating a listener. It requires a loopback bind host and exact allowed `Origin`; invalid origins return 403. A production protected HTTP deployment additionally requires the MCP OAuth 2.1 authorization model. The old HTTP+SSE transport is not implemented.

MCP request IDs/tasks are interoperability identifiers only. They never replace internal WorkOrder, Attempt, Job, or approval records. No public gateway, discovery registry, or third-agent service is included.
