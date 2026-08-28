# DQ05 — Operational and Soak Qualification

## Current evidence

The regression suite includes a short deterministic mixed-workload soak: four
independent ResearchRuns execute through planning, dispatch, verification, and
review in one SQLite/controller instance. The test asserts all runs complete
and aggregate cloud/verifier metrics match authoritative records.

This is a bounded regression signal, not a production endurance certificate.

The repository now exposes read-only storage metrics for this gate: SQLite
database and WAL bytes, regular CAS bytes/file count, and backup manifest age.
They are available through `collect_storage_metrics(...)` and Prometheus text
output. A production soak must still define acceptable growth and maximum
backup age thresholds for the target environment; collection alone is not a
freshness policy.

For repeatable evidence capture, run the probe beside the agreed workload:

```bash
.venv/bin/python scripts/dq05_storage_probe.py \
  --database /var/lib/researchd/orchestrator.db \
  --artifacts /var/lib/researchd/artifacts \
  --backup /var/lib/researchd/backups/latest \
  --samples 60 --interval-seconds 60 \
  --max-backup-age-seconds 3600 \
  --output dq05-storage-evidence.json
```

The output records the RC commit, host Python/OS, every sample, configured
thresholds, and explicit pass/fail violations. It does not certify workload
correctness; pair it with the run/audit/cloud metrics and fault-injection
evidence required below.

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
