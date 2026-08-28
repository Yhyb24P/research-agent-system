# TASK 02 Changelog

## Baseline

TASK 01 Gate passed: migration/model parity, WAL/foreign keys, optimistic concurrency, atomic transition/event writes, and DB-only restart recovery were verified.

## Files changed

- Added immutable content-addressed artifact storage and verified reads.
- Added artifact registration and complete multi-source derivation provenance.
- Added migration `0002` for derivations, approvals, grants, policy decisions, and immutable-classification trigger.
- Added deterministic capability/budget/data policy evaluation and decision recording.
- Added canonical parameter hashing and TTL/one-shot approval service.
- Added authoritative-ID ContextBuilder, cloud bundle DTO, deterministic redactor, malicious fixtures, and security tests.
- Added egress and approval policy documentation.

## Domain/API changes

Added `ApprovalStatus` and `PolicyOutcome`. ArtifactService accepts bytes plus trusted metadata; ContextBuilder accepts only authoritative IDs. ApprovalService exposes request, approve, and authorize operations. Policy Engine returns typed deterministic outcomes/effective capabilities/reason codes.

## Migration changes

Revision `0002` adds `artifact_derivations`, `approval_requests`, `approval_grants`, and `policy_decisions`, plus indexes and a trigger that prevents artifact classification changes.

## Security impact

- Store paths derive exclusively from validated SHA256, never caller filenames.
- Every read verifies bytes against the content address.
- Private/raw material can become cloud-visible only as a new CLOUD_SAFE derivation.
- Context classification precedes byte reads and fails closed.
- Redaction is defense-in-depth after authorization.
- Approval comparison is hash-bound to canonical parameters; TTL and one-shot consumption are database-enforced by conditional update.
- No real cloud call, upload, LLM summarizer, model, sandbox, or protocol adapter was added.

## Tests executed / results

- `uv run pytest`: 37 passed, including 13 TASK 02 security tests.
- `uv run mypy`: strict mode succeeded across 51 source/test files.
- `uv lock --check`: dependency lock is current.
- `uv run alembic heads/history`: one linear head at `0002`.
- Alembic model-drift check passes for newly migrated databases.
- External model/protocol import search across `src/researchd`: no matches.

## Known limitations

- Derivation transformers are trusted deterministic tools; their execution sandbox arrives later.
- Context bundles currently inline only bounded UTF-8 text from an explicit MIME allowlist.
- Policy configuration is supplied as typed inputs; configuration-file/user-policy loading is deferred.
- Artifact garbage collection and orphan cleanup are deferred.

## Deferred work

Local execution/sandbox/jobs remain TASK 03. Independent verifier, cloud model adapter, orchestration, protocol adapters, and hardening remain later tasks.

## Gate checklist

- [x] LOCAL_ONLY/SECRET bytes cannot reach mock cloud sink.
- [x] PROJECT_PRIVATE requires and substitutes a CLOUD_SAFE derivation.
- [x] Unknown classification fails closed.
- [x] Artifact corruption and classification mutation are detected/rejected.
- [x] Store paths cannot be influenced by filename traversal.
- [x] Derivation provenance is complete and atomic with derived metadata.
- [x] Approval parameter mutation, expiry, sequential/concurrent one-shot replay are rejected.
- [x] Full security/regression/static/migration checks pass.
