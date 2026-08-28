# Evidence Provenance

## Distinct records

- **Artifact**: immutable content-addressed bytes plus trusted metadata.
- **Observation**: a reproducible/measurable value produced by a named/versioned trusted producer from authoritative sources.
- **Claim**: an Executor/human/model interpretation. It may cite references but is not verification evidence.
- **VerificationResult**: typed evaluation of the exact frozen acceptance contract.
- **ReviewDecision**: later research/engineering judgment; it cannot rewrite verification.

## Observation invariant

Every Observation persists:

- `attempt_id`, stable name, and JSON value;
- at least one Artifact, execution-step, or Job source ID;
- `producer_type`, `producer_id`, and `producer_version`;
- UTC creation time.

Both Pydantic construction and a SQLite CHECK constraint reject sourceless Observations. Producer fields are required columns. Current trusted producers are command, JUnit, metric, Artifact, and reproducibility observers.

## Atomicity and refusal

Producers first validate all evidence. Missing records, wrong Attempt/type, malformed formats, non-finite metrics, duplicate reproduction identities, missing bytes, SHA mismatch, metadata mismatch, or oversized input cause `VerificationRefused`. The engine persists nothing until every required criterion has been evaluated; Observations, VerificationResult, and completion event then share one transaction.

Artifact metadata is now protected by an all-fields immutability trigger in addition to content-address verification. A metadata correction therefore requires a new record/designated migration rather than mutating historical evidence.

## Claims

`ClaimRecorder` stores Executor statements in `claims` with producer and optional supporting references. Verifier producers never query claims. The explicit negative fixture records “All tests passed” while trusted JUnit evidence contains a failure; the resulting hard verification fails.

## Trace direction

```text
WorkOrder.acceptance --SHA256--> VerificationResult
Attempt --> ExecutionStep / Artifact / Job
                    \      |      /
                     Observation
                          |
                    CriterionEvaluation
                          |
                  VerificationResult + AuditEvent
```

This is sufficient for TASK 04 verification provenance. End-to-end acceptance trace through cloud review and revisions remains TASK 06.
