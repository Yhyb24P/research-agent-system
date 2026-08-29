# DQ05 — Soak, Restart and Fault Endurance

## Objective

Demonstrate that the intended deployment remains bounded and recoverable across sustained use and repeated faults.

## Preconditions

Relevant DQ01-DQ04 hard checks must pass for the components used by the soak.

## Workload profile

Use a representative mixture of short local operations, cloud planning/review calls, workspace delegations, long jobs, approvals, verifier passes/failures, cancellations and restarts. Record the workload seed/profile so it can be repeated.

## Fault schedule

Inject at minimum: controller restart, worker restart, provider timeout, remote-agent disconnect, workspace transport failure, job-backend temporary outage, process kill during state transition boundaries, disk-pressure warning threshold, and one backup/restore drill during or immediately after the soak.

## Observe

- state transition errors;
- stuck non-terminal records;
- duplicate operations;
- orphan jobs/workspaces;
- SQLite WAL/database growth;
- CAS growth/orphans;
- memory/fd/process growth;
- event cursor monotonicity;
- reconciliation time;
- cloud call/token/cost budgets;
- audit completeness.

## Acceptance

No HARD invariant violation. Any residual non-terminal item must be explainable and recoverable through a documented reconciliation path. Resource growth must be bounded or have an explicit operational threshold/rotation procedure.
