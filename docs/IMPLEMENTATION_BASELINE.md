# Implementation Baseline

Status: TASK 08 Gate complete (2026-08-29)

## Runtime and dependency policy

- CPython: `>=3.12,<3.13`; validated with 3.12.3.
- Project/environment manager: uv 0.9.18.
- Pydantic: `>=2.13,<3` for strict DTO validation.
- SQLAlchemy: `>=2.0,<3`; Alembic: `>=1.16,<2` for authoritative V1 persistence and migrations.
- pytest: `>=8.4,<9`; jsonschema `>=4.23,<5`; mypy `>=1.17,<2` as development dependencies.
- Dependency resolution is recorded in `uv.lock`; runtime code does not depend on protocol or model-provider SDKs.

## Frozen conventions

- All persisted timestamps are timezone-aware; producers normalize to UTC.
- Entity identifiers are opaque strings with stable type prefixes (`run_`, `wo_`, `att_`, etc.). A UUID, ULID, or deterministic fixture slug may follow the prefix; no consumer parses suffix semantics.
- Artifact identifiers are content addresses: `artifact://sha256/<64 hex characters>`.
- Agent-facing DTOs are immutable and reject unknown fields.
- Mutable aggregates carry an optimistic-concurrency `version` and aware creation/update timestamps.
- State changes must be accepted by the explicit transition tables. Terminal states have no outgoing transition.
- Domain code contains no external agent-protocol types.

## Delivered through TASK08

Persistence, API behavior, model calls, policy evaluation, sandboxing, verification,
bounded orchestration, A2A/MCP boundary adapters, metrics, backup/restore, and
security/recovery regression tests are implemented and documented in the task
changelogs. Remaining work is deployment-specific validation listed in
`KNOWN_LIMITATIONS.md`, not an unimplemented core task.
