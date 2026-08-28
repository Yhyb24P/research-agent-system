# TASK 06 Changelog

## Baseline

TASK 05 Gate passed with safe cloud context, typed Cloud Lead calls, bounded repair, and durable interaction accounting.

## Changes

- Added migration `0006` and durable `PlanRecord`/`ReviewDecisionRecord` lineage.
- Added run iteration/cloud-call budgets, cancellation intent, WorkOrder revision metadata, and approval grant linkage.
- Implemented `ResearchOrchestrator` for plan → policy → dispatch → execute → verify → review → accept/revise/human/abort.
- Added explicit approval and human-resolution commands, unchanged-WorkOrder Attempt retry, cancellation, recovery reconciliation, and structured status/event APIs plus CLI parser.
- Added fake NaN-loop, revision, human pause, approval pause/resume, restart reconciliation, cancellation, and max-budget integration coverage.

## Security and authority

- Every model call follows a committed workflow event and uses the existing authoritative CloudContextBuilder.
- Cloud proposals never grant capabilities; deterministic policy and hash-bound approvals decide effective access.
- Verifier state, not executor claims or cloud review, controls `REVIEW_READY` and acceptance.
- Revisions preserve parent lineage; retries preserve WorkOrder semantics while creating a new Attempt.
- Cancellation and restart are controller operations over durable records; raw agent conversation is not state.

## Known limitations

- The local control API is an in-process facade/CLI surface; a network HTTP server remains intentionally deferred.
- The generic ExecutionDriver/VerificationDriver interfaces require deployment-specific adapters for real repositories and scheduler jobs.
- Run wall-clock deadline enforcement and automatic retry policy are represented by bounded counters and explicit commands; scheduler-specific cancellation/reconciliation remains backend-specific.

## Gate checklist

- [x] Full fake E2E reaches terminal completion with provenance events.
- [x] Verification failure creates a linked revision and cannot be accepted.
- [x] Human-required and approval-required paths pause durably and resume only explicitly.
- [x] Restart after persisted execution result reconciles without duplicate execution/model reconstruction.
- [x] Cancellation prevents new dispatch and cancels active attempts.
- [x] Iteration/cloud-call bounds stop the loop visibly.
- [x] Core has no A2A/MCP/public-service implementation; protocol adapters remain TASK07.
