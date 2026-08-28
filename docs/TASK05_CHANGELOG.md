# TASK 05 Changelog

## Baseline

TASK 04 Gate passed with immutable evidence, independent deterministic verification, and a hard storage-layer acceptance precondition.

## Changes

- Classified Observations and VerificationResults for egress and made both immutable in migration `0005`.
- Extended safe context construction to authoritative Artifact, Observation, and Verification selections with recursive redaction and canonical serialization.
- Added typed PlanProposal, WorkOrderProposal, EvidenceRequest, and ReviewDecision cloud operations.
- Added a provider-neutral `CloudModel`, a direct HTTPS OpenAI-compatible adapter, bounded schema repair, budgets, token/cost accounting, and explicit external-wait state.
- Added durable `agent_interactions` and bracketed cloud audit events.
- Added mock/configured-provider integration tests covering egress, policy/verifier non-authority, malformed output, timeout, accounting, tracing posture, and transport limits.

## Security impact

- Cloud Lead cannot accept caller-authored context or reach local execution interfaces.
- `LOCAL_ONLY`, `SECRET`, unsafe-source, and mismatched-scope evidence fails before provider invocation.
- Provider URL and response size are bounded; tools, hosted storage, and conversation state are disabled.
- Malformed raw output is not persisted, and repairs are count-bounded.
- Cloud recommendations remain subordinate to policy and hard verification.

## Known limitations

- The configured-provider test exercises the real HTTP adapter against a deterministic mock transport, not a paid live endpoint.
- Provider-side retention is outside this process and must be controlled by deployment/provider policy.
- TASK 05 records interaction-level waiting; run-level retries and orchestration are TASK 06.

## Tests executed / results

- `.venv/bin/pytest`: 75 passed, including 9 Cloud Lead integration tests.
- `.venv/bin/mypy`: strict mode succeeded across 76 source/test files.
- `uv lock --check`: 32-package lock is current.
- Alembic fresh-database upgrade/check: one linear head at `0005`, with no model drift.
- Runtime import scan: no A2A, MCP, LangGraph, or Anthropic integration imports.

## Gate checklist

- [x] Provider receives only reconstructed, redacted `PUBLIC`/`CLOUD_SAFE` context.
- [x] Structured output has bounded repair and explicit failure.
- [x] Forbidden proposed capabilities are denied by deterministic policy.
- [x] Cloud acceptance cannot override failed hard verification.
- [x] Timeout/unavailability is explicit `WAITING_EXTERNAL`.
- [x] Tracing/tools/conversation/provider storage are disabled in the baseline adapter.
- [x] Token, cost, request identity, status, and audit events are persisted.
