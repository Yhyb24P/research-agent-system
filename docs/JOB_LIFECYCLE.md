# Durable Local Job Lifecycle

## Typed submission

Public `JobSpec` identifies a configured `job_type`; it does not contain a shell command. The trusted local backend maps that type to an internal argv vector. Local jobs run inside a no-network bubblewrap namespace with cleared environment, isolated filesystem visibility, and memory limit. GPU requests are rejected because this backend does not claim GPU isolation.

## Submission and idempotency

The submission sequence is deliberately crash-aware:

1. Insert a unique `jobs.operation_id` reservation in SQLite as `CREATED` and append `JOB_SUBMISSION_RESERVED` in the same transaction.
2. Atomically create the backend operation directory. Its existence is the backend idempotency claim; a second submit cannot launch another runner.
3. Start a detached trusted runner and persist its PID plus Linux process start-time identity.
4. Runner starts the sandboxed job and atomically writes durable `status.json` records.
5. Persist the native handle/state and `JOB_SUBMITTED` event.

A duplicate manager submission returns the existing Job record. If the controller crashes after the side effect but before step 5, restart reconciliation locates the backend by `operation_id` and fills the missing native handle without resubmission. If it crashes after the DB reservation but the backend operation cannot be found, reconciliation marks the Job `LOST`; it does not automatically resubmit.

This is at-most-one local launch under an intact operation directory, not a claim of distributed exact-once execution. Backend directory loss, filesystem corruption, or scheduler-specific ambiguity produces `LOST` and requires policy/human reconciliation.

## Durable status and PID safety

The detached runner writes `RUNNING`, then `SUCCEEDED` or `FAILED`, using temporary-file replacement. Before status exists, reconciliation validates both PID and `/proc/<pid>/stat` start time to avoid treating a reused PID as the original runner.

Every reconciliation writes `JOB_STATUS_CHANGED`. Cancellation first persists `CANCEL_REQUESTED` plus `JOB_CANCEL_REQUESTED`, signals the detached runner process group, waits boundedly, writes durable `CANCELLED`, then records the final database state/event. Existing artifacts and records are retained.

## Restart algorithm

On controller startup:

1. query nonterminal Job records from SQLite;
2. query the backend by native handle, or by operation ID when the handle is absent;
3. validate durable status/runner identity;
4. map backend state to the authoritative Job record and append an audit event;
5. use `LOST` when identity cannot be established;
6. never infer status from agent prose and never automatically duplicate a lost submission.
