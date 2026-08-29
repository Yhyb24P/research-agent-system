# DQ02 — Agent Runtime and Invocation Lifecycle Qualification

## Objective

Qualify the real Agent runtime path used for delegated work while preserving
the trusted control plane as the sole owner of authoritative workflow state.

## Required scenarios

- Agent registration, runtime lease acquire/renew/release and selection conflict;
- dispatch a long-running invocation and persist its external identity;
- controller restart while an invocation is running;
- Agent runtime restart while an invocation is running;
- status reconciliation after transient runtime loss;
- cancellation before start, during execution and after external completion;
- duplicate dispatch with the same operation/idempotency identity;
- typed failure, malformed result, timeout and forced termination;
- output/log truncation and artifact collection limits;
- concurrent invocation and lease contention;
- remote Agent unreachable, reconnect and stale-result delivery;
- no implicit fallback to an unauthorized Agent or runtime.

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
