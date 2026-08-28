# TASK 07 Changelog

## Baseline

TASK 06 Gate passed with a durable bounded orchestrator, restart recovery, local status controls, and a provenance-complete fake E2E.

## Changes

- Added A2A 1.0 wire models, pinned Agent Card generator/example, outbound task mapping, deterministic idempotency, and terminal-task refinement.
- Added optional MCP 2025-11-25 stdio JSON-RPC façade over a native service and a loopback/Origin-validating Streamable HTTP test façade.
- Added remote mapping columns/indexes to `agent_interactions` in migration `0007`.
- Added adapter contract tests and documented protocol/security boundaries.

## Security and authority

- A2A/MCP IDs remain opaque adapter metadata; internal run/work-order/attempt/job state stays authoritative.
- No public A2A listener or internet-exposed MCP service is created.
- MCP handlers contain no business logic and cannot grant capabilities, approvals, or acceptance.
- A2A dispatch reserves local idempotency state before outbound I/O; terminal refinement creates a new task/mapping.

## Known limitations

- Wire models intentionally avoid a third-party A2A/MCP SDK; provider/server conformance beyond the tested subset requires deployment contract tests.
- HTTP adapter is a policy test façade, not a production server; authentication and OAuth deployment are deferred.

## Gate checklist

- [x] A2A Agent Card exposes `supportedInterfaces` and protocol version `1.0.0`.
- [x] Terminal A2A refinement creates a new task mapping while preserving context.
- [x] Duplicate dispatch reuses durable internal mapping/idempotency key.
- [x] MCP stdio delegates to a native service.
- [x] Invalid MCP HTTP Origin is rejected and non-loopback bind is refused.
- [x] Core tests remain independent of adapter imports.

## Tests executed / results

- `.venv/bin/pytest`: 86 passed, including A2A/MCP contract tests.
- `.venv/bin/mypy`: strict mode succeeded across 89 source/test files.
- `uv lock --check`: current 32-package lock.
- Alembic fresh-database upgrade/check: one linear head at `0007`, no model drift.
- Core import scan confirms adapter modules are not imported by domain, orchestrator, policy, verifier, executor, or cloud-agent code.
