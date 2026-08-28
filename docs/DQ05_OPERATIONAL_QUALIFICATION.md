# DQ05 — Operational and Soak Qualification

## Current evidence

The regression suite includes a short deterministic mixed-workload soak: four
independent ResearchRuns execute through planning, dispatch, verification, and
review in one SQLite/controller instance. The test asserts all runs complete
and aggregate cloud/verifier metrics match authoritative records.

This is a bounded regression signal, not a production endurance certificate.

## Production qualification still required

Run a target-environment workload with mixed success/failure, approvals,
cancellation, provider 429/5xx, local model outage, controller restart,
executor restart/reconciliation, and backup/restore during operation. Record
queue depth, stuck-state age, transition failures, cloud latency/error rates,
token/cost totals, disk/WAL/CAS growth, backup freshness, and cancellation
latency. Define the workload duration or operation count, pass thresholds, and
the exact RC/environment manifest before starting.

Any implementation failure requires a minimal fix, a new RC, and rerunning the
affected DQ gate; do not mix unreviewed feature work into a soak run.
