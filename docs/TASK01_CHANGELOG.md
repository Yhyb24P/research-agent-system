# TASK 01 Changelog

## Baseline

TASK 00 Gate passed after runtime and static nominal ID separation, schema validation, transition-table review, and the full baseline suite.

## Files changed

- Added SQLAlchemy 2/Alembic dependencies and lock updates.
- Added SQLite engine configuration, UTC type, authoritative records, repositories, recovery queries, and transactional transition service.
- Added Alembic environment and initial `0001` migration.
- Added migration, WAL, concurrency, restart recovery, terminal-state, append-only, and transaction atomicity tests.
- Added `docs/STORAGE_MODEL.md`.

## Domain/API changes

Added frozen `JobState`. Storage repositories expose explicit add/get and nonterminal recovery queries. `TransactionalTransitionService` requires entity ID, expected version, target state, actor/correlation metadata, and an event type.

## Migration changes

Revision `0001` creates workspaces, research runs, WorkOrders, attempts, jobs, artifact metadata, audit events, constraints, foreign keys, and recovery/query indexes.

## Security impact

- SQLite foreign keys and full synchronous WAL writes are enabled on every application connection.
- State transitions use conditional version/state updates.
- The transition and audit event share one transaction and fail together.
- Event repository offers append/query operations only; duplicate event IDs are rejected.
- No model, cloud, protocol, sandbox, secret, or host execution surface was added.

## Tests executed / results

- `uv run pytest`: 24 passed (7 persistence integration tests plus 17 TASK 00 tests).
- `uv run mypy`: success across 41 source/test files in strict mode.
- `uv lock --check`: dependency lock is current.
- `uv run alembic heads/history`: one linear head, revision `0001`.
- Alembic `command.check`: migrated schema has no model drift.
- Forbidden protocol/framework search across domain/storage: no matches.

## Known limitations

- SQLite remains a single-host V1 database.
- TASK 01 provides Job metadata/recovery queries, not scheduler submission or reconciliation behavior.
- Storage callers must use the transition service for authoritative lifecycle changes; a broader application service boundary arrives with orchestration work.
- Backup/restore and crash fault injection beyond clean close/reopen remain TASK 08 gates.

## Deferred work

Artifacts bytes/provenance, policy, egress, and approvals remain TASK 02. Execution, verification, agents, orchestration, and adapters remain later gated tasks.

## Gate checklist

- [x] Empty DB migrates to revision `0001` and matches ORM metadata.
- [x] SQLite WAL and foreign keys are active.
- [x] Exactly one concurrent expected-version transition wins.
- [x] Restart recovers nonterminal records, Jobs, idempotency mapping, and committed events without conversation state.
- [x] Terminal WorkOrder cannot reopen through the transition service.
- [x] Transition and event commit/rollback atomically.
- [x] Full tests and strict static checks pass.
