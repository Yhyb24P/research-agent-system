# DQ02 — GPU and Job Backend Qualification

## Objective

Qualify the real GPU/job execution path used for long research workloads without claiming stronger isolation or exactly-once semantics than the implementation provides.

## Required scenarios

- admission/lease acquire, renew/release and conflict behavior;
- submit long job and persist external job identity;
- controller restart while job is running;
- executor/worker restart while job is running;
- status reconciliation after transient backend loss;
- cancel before start, during execution and after external completion;
- duplicate submit with the same operation/idempotency identity;
- GPU OOM, non-zero exit, preemption/kill and host reboot simulation where feasible;
- stdout/stderr/log truncation and artifact collection limits;
- multi-job resource contention;
- remote GPU host unreachable and reconnect;
- no implicit fallback to an unauthorized cloud or alternate model/backend.

## Required metrics

Record queue latency, start latency, reconciliation latency, cancel latency, duplicate authoritative side-effect count, orphan-job count and GPU lease conflicts.

## HARD acceptance

- no unexplained duplicate authoritative job side effect;
- no lost authoritative job identity after restart;
- cancellation/reconciliation outcome is auditable;
- logical GPU admission is described as logical admission, not hard isolation, unless independently proven;
- backend outage fails closed instead of broadening execution authority.
