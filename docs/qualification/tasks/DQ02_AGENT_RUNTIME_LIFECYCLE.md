# DQ02 — Agent Runtime and Invocation Lifecycle Qualification

## Objective

Qualify the real Agent runtime path used for delegated work while preserving
the trusted control plane as the sole owner of authoritative workflow state.

## Required scenarios

| ID | Scenario | Severity |
|---|---|---|
| DQ02-01 | registration and owned runtime lease acquire/renew/release | HARD |
| DQ02-02 | concurrent lease conflict is exclusive and durable | HARD |
| DQ02-03 | dispatch persists the external invocation identity | HARD |
| DQ02-04 | controller restart retains bound invocation for reconciliation | HARD |
| DQ02-05 | runtime restart exposes then reconciles an orphan | HARD |
| DQ02-06 | cancel before dispatch, during execution and after completion | HARD |
| DQ02-07 | duplicate dispatch creates one authoritative invocation | HARD |
| DQ02-08 | typed failure, malformed result, timeout and forced termination | HARD |
| DQ02-09 | output and artifact admission limits fail closed | HARD |
| DQ02-10 | concurrent invocation respects Agent capacity | HARD |
| DQ02-11 | unreachable/reconnect and stale-result delivery | HARD |
| DQ02-12 | outage never falls back to an unauthorized runtime | HARD |

## Required metrics

Record queue latency, start latency, reconciliation latency, cancel latency,
duplicate authoritative side-effect count, orphan invocation count and runtime
lease conflicts.

## HARD acceptance

- no unexplained duplicate authoritative workflow side effect;
- no lost AgentInvocation or external runtime identity after restart;
- cancellation/reconciliation outcome is auditable;
- an Agent result cannot directly mutate or verify authoritative state;
- runtime outage fails closed instead of broadening execution authority.

## Executable matrix and evidence

Migration `0018` persists an owned runtime lease plus dispatch, external-start,
reconciliation, cancellation and deadline timestamps on each canonical
`AgentInvocation`. Runtime lease conflicts are append-only records. A bound
external identity is immutable, and a terminal or mismatched result is stale
rather than authoritative.

Run the matrix and retain the sanitized report outside the repository:

```bash
mkdir -p <evidence-root>/DQ02
DQ02_REPORT=<evidence-root>/DQ02/runtime-lifecycle-report.json \
  uv run pytest -q tests/qualification/test_dq02_runtime_lifecycle.py \
  --junitxml=<evidence-root>/DQ02/runtime-lifecycle-junit.xml
```

The report records the required latency metrics, lease conflicts, orphan count,
duplicate authoritative side effects and auditable lifecycle event types. A
passing observation remains `IN_PROGRESS` until separately accepted under the
Gate policy.
