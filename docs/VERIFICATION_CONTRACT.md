# Independent Verification Contract

## Authority boundary

The Verifier is trusted deterministic controller code. It does not accept an Executor boolean, prose claim, or cloud review as proof. Its inputs are the frozen WorkOrder acceptance contract plus authoritative Attempt, execution-step, Artifact, and Job records read through trusted services.

The caller supplies criteria, but `VerifierEngine` normalizes and hashes them and requires an exact semantic match with `work_orders.contract.acceptance`. This prevents a caller from weakening criteria after dispatch.

## Criterion types

### Command

Reads a completed persisted execution step and compares its trusted exit code. When `junit_artifact_id` is present, it also verifies the Artifact content hash/type/Attempt provenance, parses XML with DOCTYPE rejected, and requires at least one test with zero failures and errors. Exit code zero cannot hide a failing JUnit result.

### Metric

Reads a registered `metrics` Artifact belonging to the Attempt, verifies identity/hash/size, parses strict JSON with non-finite values rejected, and compares finite numeric values using decimalized exact boundary semantics for `==`, `!=`, `>`, `>=`, `<`, and `<=`.

### Artifact

Queries Artifact metadata by Attempt and type, verifies each content address and byte count, then enforces `min_count`. Zero matching Artifacts is a valid failed criterion; corrupt or inconsistent Artifact provenance causes verification refusal.

### Reproducibility

Consumes distinct registered `reproducibility` Artifacts. Each must contain a unique `run_id` and boolean `success`. Passing requires at least the configured independent run count and required successes. Duplicate source IDs or run IDs are refused.

## Hard and advisory aggregation

Every criterion has `severity=hard|advisory`, defaulting to hard.

- Any hard failure makes `overall=fail`.
- Advisory failures remain visible but cannot turn a hard-pass result into failure.
- A source/provenance/integrity problem raises `VerificationRefused`; no partial Observations or VerificationResult are committed.
- A completed evaluation persists all Observations, the result, acceptance hash, verifier version, and `VERIFICATION_COMPLETED` event atomically.

## REVIEW_READY Gate

The generic storage transition service enforces the Gate, not an orchestrator convention. `VERIFYING → REVIEW_READY` requires:

- the latest Attempt for the WorkOrder;
- its latest VerificationResult;
- `valid=true` and `overall=pass`;
- a result acceptance hash matching the current frozen WorkOrder contract.
- a one-to-one criterion-evaluation set whose severities match the contract and whose hard results all pass.

Without all conditions, it raises `TransitionPreconditionFailed`. It does not trust an `overall=pass` summary with missing/forged criterion details. Consequently a later Cloud Lead review cannot override failed hard verification because the WorkOrder cannot reach review readiness.

## Bounded evidence

Each evidence Artifact is bounded (16 MiB by default), hash-verified, size-verified, type-checked, and Attempt-bound. Larger scientific outputs require a separately registered deterministic summary/observation producer rather than unbounded verifier input.
