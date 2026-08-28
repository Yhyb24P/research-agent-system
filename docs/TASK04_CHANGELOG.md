# TASK 04 Changelog

## Baseline

TASK 03 Gate passed with real bubblewrap isolation, worktree provenance, persisted execution idempotency, no-cloud-fallback Local Executor, and durable operation-ID Job reconciliation.

## Files changed

- Extended all acceptance criteria with hard/advisory severity and Command JUnit evidence.
- Added normalized acceptance fingerprinting.
- Added Observation, Claim, and VerificationResult persistence and migration `0004`.
- Added immutable Artifact metadata trigger.
- Added trusted command/JUnit/metric/Artifact/reproducibility producers and VerifierEngine.
- Added ClaimRecorder and storage-enforced REVIEW_READY verification precondition.
- Added verification fixtures, integration tests, and verification/provenance documents.

## Domain/API changes

Criterion evaluations now include severity and stable reason code. VerificationResult carries acceptance SHA256, verifier version, and validity. `VerificationInputs` maps typed criteria to authoritative Artifact IDs; claims remain a separate write path.

## Migration changes

Revision `0004` creates `observations`, `claims`, and `verification_results`, their indexes/source constraint, and an Artifact metadata immutability trigger.

## Security impact

- Executor/cloud prose cannot satisfy acceptance.
- Criteria cannot be weakened after WorkOrder dispatch without changing the hash and being refused.
- Missing/corrupt/mismatched sources fail closed without partial evidence.
- Hard failure prevents REVIEW_READY in the generic storage transition layer.
- JUnit parser rejects DOCTYPE and malformed/counter-invalid XML.
- Evidence reads have byte, type, Attempt, hash, size, and metadata checks.
- No Cloud Lead, A2A, or MCP was added.

## Tests executed / results

- `uv run pytest`: 66 passed, including 15 verifier integration tests.
- `uv run mypy`: strict mode succeeded across 68 source/test files.
- `uv lock --check`: dependency lock is current.
- `uv run alembic heads/history`: one linear head at `0004`; migrated-schema model drift checks pass.
- A2A/MCP/LangGraph/Anthropic import scan across runtime source: no matches.

## Known limitations

- Current producers cover command/JUnit, scalar JSON metrics, Artifact count/type, and boolean independent-run aggregation. Domain-specific scientific validators remain future plugins behind the same interface.
- Reproducibility evidence proves independent recorded run IDs and outputs; scheduler/environment equivalence checks require later domain-specific producers.
- Cloud research review and accepted-result trace are deferred to TASK 05/06.

## Deferred work

Cloud-safe Lead context/model calls remain TASK 05. Orchestration, review/revision acceptance, adapters, and hardening remain later Gates.

## Gate checklist

- [x] Executor/cloud claim cannot override failed JUnit/hard evidence.
- [x] Missing/corrupt provenance refuses without partial result.
- [x] Metric boundary operators are tested.
- [x] Artifact criterion verifies identity/hash/type/size/count.
- [x] Reproducibility aggregates distinct run IDs and source Artifacts.
- [x] Every persisted Observation has producer and source.
- [x] REVIEW_READY requires latest matching valid hard-pass result and a complete criterion set.
- [x] Forged empty `overall=pass` summary is rejected.
- [x] Full verification/regression/static/migration checks pass.
