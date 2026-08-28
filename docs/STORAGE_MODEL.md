# Storage Model

## Authority and transaction boundary

SQLite is the authoritative V1 workflow store. Agent conversations and prose are never replayed to reconstruct state. `TransactionalTransitionService` validates a frozen domain transition, performs a version-guarded SQL update, and appends its audit event in the same database transaction. A failure in either write rolls back both.

Every mutable aggregate has `version`, `created_at`, and `updated_at`. ResearchRuns additionally persist iteration/cloud-call ceilings, counters, and cancellation intent. Transitions require `expected_version`; the update predicate includes ID, state, and version, so concurrent callers cannot both win. Timestamps are persisted as timezone-aware UTC ISO-8601 text because SQLite's native datetime handling does not preserve offsets reliably.

## SQLite configuration

Every application connection enables:

- `foreign_keys=ON`
- `journal_mode=WAL`
- `synchronous=FULL`
- `busy_timeout=5000`

The database remains single-host/single-controller oriented. PostgreSQL and distributed queues remain explicitly deferred.

## Tables

| Table | Authority | Important constraints/indexes |
|---|---|---|
| `workspaces` | Workspace identity | primary ID, version/timestamps |
| `research_runs` | Goal lifecycle | workspace FK, state indexes |
| `plans` | Structured Cloud Lead plan proposals | Run index, version/timestamps |
| `work_orders` | Immutable-after-dispatch execution contract and state | run/parent FKs, unique idempotency key, run+state index |
| `attempts` | One execution of an unchanged WorkOrder | WorkOrder FK, state indexes, terminal timestamp |
| `jobs` | Durable external job identity | Attempt FK, unique operation ID, state indexes |
| `artifacts` | Immutable content metadata | unique SHA256, nonnegative size, Attempt FK, classification index |
| `audit_events` | Append-only transition/audit history | immutable event ID, run/entity/correlation indexes |
| `artifact_derivations` | Immutable source-to-derived provenance | composite derived/source identity and transformation hashes |
| `approval_requests` / `approval_grants` | Hash/TTL-bound authorization | unique grants, expiry/hash indexes |
| `policy_decisions` | Deterministic policy audit | Run/WorkOrder references and reason codes |
| `execution_steps` | Capability-operation idempotency | unique request ID and parameters hash |
| `executor_dispatches` | Attempt dispatch idempotency | one dispatch result per Attempt |
| `attempt_worktrees` | Worktree/environment provenance | one unique worktree record per Attempt |
| `observations` | Trusted measurable evidence | Attempt index and mandatory source CHECK |
| `claims` | Untrusted interpretations | separate Attempt-linked records |
| `verification_results` | Frozen acceptance evaluation | WorkOrder/Attempt indexes and acceptance hash |
| `review_decisions` | Structured Cloud Lead review recommendations | Run/WorkOrder indexes, interaction/evidence references |
| `agent_interactions` | Cloud request lifecycle, accounting, and optional remote protocol mapping | Run/status/A2A-task indexes, bundle hash, structured result, token/cost fields |

`WorkOrderRecord.contract` stores the validated immutable dispatch contract as JSON. TASK 01 does not yet implement dispatch immutability enforcement; the transition service is the authoritative mutation path, and TASK 03 will bind dispatch behavior to this record.

## Recovery primitives

Repositories expose active queries for ResearchRuns, WorkOrders, Attempts, and Jobs. On restart the controller opens the database, reads nonterminal records, and uses persisted event/entity identifiers for reconciliation. It does not ask either model to reconstruct work.

Terminal sets used by recovery:

- ResearchRun: `COMPLETED`, `FAILED`, `CANCELLED`
- WorkOrder: `ACCEPTED`, `REVISION_REQUIRED`, `FAILED`, `CANCELLED`
- Attempt: `SUCCEEDED`, `FAILED`, `CANCELLED`
- Job: `SUCCEEDED`, `FAILED`, `CANCELLED`, `LOST`

## Migration policy

Alembic revision `0001` creates the authoritative base schema. Revisions through `0005` add provenance, execution, verification, evidence classification, immutability triggers, and cloud interaction accounting. Application code must use Alembic rather than `Base.metadata.create_all` for durable databases. Future schema changes require forward migrations and explicit data-risk review.
