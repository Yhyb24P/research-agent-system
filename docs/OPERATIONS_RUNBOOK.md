# Operations Runbook

## Start and inspect

The V1 process is a local modular monolith. Open the SQLite database through the configured session factory, then use `LocalControlAPI.run_status(run_id)` and `.events(run_id)` (or the CLI status/events commands) to inspect structured state. Do not inspect raw model conversation as workflow state.

Metrics are collected from authoritative records:

```python
from researchd.observability import collect_metrics
snapshot = collect_metrics(sessions)
print(snapshot.as_dict())
print(snapshot.prometheus())
```

The snapshot covers cloud calls/tokens/cost/status, Job states, policy outcomes, approval statuses, verifier outcomes, and review decisions. Labels contain only stable state names, never prompts, secrets, paths, or artifact bytes.

## Restart and recovery

1. Reopen the same SQLite file with WAL enabled.
2. Construct the same backend adapters and `RecoveryCoordinator`.
3. Call `recover_run(run_id)` for each nonterminal run.
4. Resume with `await orchestrator.run(run_id)`.

Recovery reconciles persisted Job handles and executor dispatch results. It does not resubmit `LOST` jobs automatically and does not ask a model to reconstruct state. A crash after an external side effect and before its database commit can leave an at-least-once window; inspect the operation-id mapping before any manual retry.

## Cancellation and pauses

Use `await LocalControlAPI.cancel_run(run_id)` for controller cancellation. The controller records cancellation intent, stops new dispatch, requests active backend cancellation, and preserves artifacts/audit events. `WAITING_HUMAN`, `WAITING_EXTERNAL`, and `WAITING_APPROVAL` require their explicit resolution commands; an HTTP client disconnect is not interpreted as cancellation.

## Backup and restore

Create a snapshot while the controller is quiesced or at an agreed consistency point:

```python
from researchd.backup import backup_snapshot, restore_snapshot
manifest = backup_snapshot(db_path, artifact_store_root, backup_dir)
restore_snapshot(backup_dir, restored_db, restored_artifacts)
```

The operation checkpoints SQLite WAL, uses SQLite's online backup API, queries the snapshot for referenced artifact hashes, copies only those content-addressed bytes, and writes SHA256 checksums for the database and every artifact file. Restore also checks database/artifact reference equality, refuses incomplete/tampered snapshots, and refuses to overwrite existing destinations. Keep snapshots encrypted and access-controlled outside this process.

## Incident handling

- `CLOUD_UNAVAILABLE`: leave the run in `WAITING_EXTERNAL`; verify provider/network status, then resume.
- `CLOUD_SCHEMA_INVALID`: inspect structured interaction reason/attempt count; revise or human-resolve, never paste raw output into state.
- `EXECUTOR_MODEL_UNAVAILABLE`: keep local-only semantics; do not route context to cloud.
- `LOST` Job: reconcile native scheduler identity and require an explicit operator decision; do not blindly resubmit.
- Artifact hash mismatch: quarantine the store/path, restore from a verified snapshot, and preserve the failed audit trail.
- Policy/approval denial: inspect reason codes and exact parameter hash; changing parameters requires a new approval.

## Semantics and limits

The local durable Job backend provides persisted operation-id deduplication plus reconciliation, not a universal exactly-once guarantee. GPU isolation, provider retention, scheduler-specific semantics, filesystem race resistance, and off-host backup durability must be reviewed for the deployment environment.
