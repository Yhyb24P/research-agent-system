"""Run the optional MCP stdio façade with a native service supplied by the host."""

import json
import sys

from researchd.adapters.mcp import MCPStdioAdapter


class DemoTestService:
    def run_target(self, target: str) -> dict[str, object]:
        return {"target": target, "status": "delegated-to-native-service"}


def main() -> int:
    adapter = MCPStdioAdapter(DemoTestService())
    for line in sys.stdin:
        response = adapter.handle_line(line)
        if response:
            print(response, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
