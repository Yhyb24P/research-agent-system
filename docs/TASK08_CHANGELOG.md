# TASK 08 Changelog

## Baseline

TASK 07 Gate passed with pinned A2A 1.0.0/MCP 2025-11-25 adapters isolated from the core domain.

## Changes

- Added privacy-preserving structured metrics for cloud usage, Jobs, policy, approvals, verification, and review outcomes.
- Added deterministic one-shot `FaultInjector` for integration and deployment fault tests.
- Added SQLite online-backup plus content-addressed artifact snapshot/restore with database and per-file SHA256 manifests.
- Added explicit SSH-key/prompt-injection, GPU-budget, vLLM-timeout, unchanged-WorkOrder retry, and bounded real Git-repository pilot tests.
- Added operations runbook, security matrix report, pilot report, known limitations, and final architecture diagram.
- Updated README and implementation baseline to reflect TASK00–TASK08 delivery.

## Security and operational impact

- Failures are surfaced as structured statuses/reason codes and metrics; no raw prompt, secret, or artifact bytes enter metrics.
- Backup refuses tampered/incomplete snapshots and never overwrites restore destinations.
- Pilot exercises real Git worktree, Bubblewrap sandbox, capability broker, local executor, deterministic verifier, Cloud Lead review, and complete provenance trace.
- Documentation explicitly classifies GPU isolation, provider retention, scheduler semantics, filesystem races, and off-host backup as deployment release blockers.

## Tests executed / results

- `.venv/bin/pytest`: 95 passed.
- `.venv/bin/mypy`: strict mode succeeded across 95 source/test files.
- `uv lock --check`: current 32-package lock.
- Alembic fresh-database upgrade/check: one linear head at `0007`, no model drift.
- Acceptance matrix A01–A30 evidence is mapped in `docs/SECURITY_TEST_REPORT.md`.

## Gate checklist

- [x] Structured operational metrics and Prometheus text output.
- [x] Cloud/vLLM timeout and fault-injection coverage.
- [x] Controller/Job restart, duplicate dispatch, cancellation, artifact corruption, and approval mutation regressions.
- [x] SQLite + artifact backup/restore with checksums.
- [x] Bounded real repository pilot reaches accepted result with complete trace.
- [x] Operations, security, pilot, limitations, and final architecture documents delivered.
- [x] No additional agents, public A2A endpoint, Kubernetes, automatic push, or security-gate weakening.
