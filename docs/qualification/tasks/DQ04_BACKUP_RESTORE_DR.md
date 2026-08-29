# DQ04 — Backup, Restore and Disaster Recovery Qualification

## Objective

Prove that authoritative SQLite state plus CAS artifacts can be backed up, transferred/stored according to policy, restored to a clean target and validated without silent divergence.

The backup subsystem already implements SQLite online backup, WAL
checkpoint, referenced-CAS collection, checksum manifest, clean-path restore
and integrity verification (`src/researchd/backup/snapshot.py`). DQ04 is
therefore not a rewrite: it establishes an executable qualification matrix
over the real restore invariants and fixes only evidence-backed defects.

## Executable scenario matrix (test design, DQ04-00)

| ID | Scenario | Implementation entry | Current coverage | Required work |
|---|---|---|---|---|
| DQ04-01 | Idle online backup → manifest → clean restore → integrity PASS | `backup_snapshot` / `restore_snapshot` / `check_restored_snapshot` | executable test PASS | repeat on frozen candidate and retain evidence |
| DQ04-02 | Backup during concurrent AuditEvent append, Artifact creation, WorkOrder/Attempt update | SQLite online backup API | executable concurrent-writer test PASS | repeat on frozen candidate and retain evidence |
| DQ04-03 | Missing referenced CAS object | transactional backup fails closed | executable test PASS; failed backup leaves no partial destination | repeat on frozen candidate |
| DQ04-04 | Corrupted CAS object (payload tampered, path intact) | source and snapshot payload hashes recomputed | source-backup and snapshot-restore tests PASS | repeat on frozen candidate |
| DQ04-05 | Corrupted database snapshot (byte/page damage) | checksum plus SQLite integrity/schema checks | checksum and rewritten-manifest corruption tests PASS | repeat on frozen candidate |
| DQ04-06 | Manifest tamper (database hash, artifact hash, schema revision, candidate metadata) | strict current-format parser and expected candidate binding | executable field-tamper matrix PASS | repeat on frozen candidate |
| DQ04-07 | Snapshot tree escape (symlink, hardlink, external target, path traversal) | exact tree, canonical CAS paths, single-link files, symlink refusal | executable symlink/hardlink/traversal tests PASS | repeat on frozen candidate |
| DQ04-08 | Clean-host restore with full relationship validation | all authoritative tables/triggers, FK check and plain-reference checks | executable clean-restore and divergence tests PASS | repeat on frozen candidate |
| DQ04-09 | Primary-loss drill (primary DB + local CAS unavailable; off-host snapshot only) | isolated primary-loss test plus `scripts/dq04_dr_probe.py` metrics | isolated drill PASS | execute against actual off-host backend and retain RPO/RTO evidence |
| DQ04-10 | Post-restore recovery and continued operation | invocation recovery plus restored-controller continuation | runtime invocation is failed closed and new WorkOrder reaches completion in test | add actual workspace/worktree/job recovery drill on deployment environment |
| DQ04-11 | Retention / rotation / deletion | `backup/retention.py` | executable component and temp-root drill implemented | run against the production-intent snapshot store and retain its evidence |
| DQ04-12 | Off-host protection record | `schemas/dq04_offhost_protection.schema.json` | strict non-secret record contract and example implemented | replace example values with actual production-intent controls and independently review the record |

## Candidate-affecting implementation status

These changes touch trusted persistence/backup semantics. Per the mainline
plan they require a new RC and re-qualification of affected gates.

1. **Legacy compatibility removed.** Restore accepts only current format 3;
   the migration-0008 restore/upgrade test has been removed. No old-format or
   old-schema compatibility shim remains.
2. **CAS inventory explicit.** Successful manifests record orphan digests;
   missing and corrupt referenced objects fail backup. Restore health reports
   missing/orphan/corrupt counts.
3. **Relationship and schema verification expanded.** All authoritative
   tables and integrity triggers must exist; declared foreign keys and the
   remaining plain-string references are checked.
4. **Candidate identity bound.** Manifest format 3 requires commit and RC tag;
   restore also requires an independently supplied expected identity.
5. **Retention implemented.** Planning is side-effect free; application is
   bounded to validated direct-child snapshots, with protected restore points.
6. **Off-host protection contract implemented.** The schema requires actual
   access control, encryption, key ownership, authorization, retention and
   deletion evidence and forbids secrets. The production-intent record remains
   an operational evidence task.

## Required metrics

Backup duration, backup size, RPO achieved in the drill, restore
duration/RTO, validation duration and number of missing/orphan/corrupt
references. Metrics bind to the exact candidate commit/tag and environment
fingerprint of the evidence run.

## HARD acceptance

A restore is not successful until database schema/version, CAS references,
artifact hashes and authoritative relationships pass integrity checks.
Merely opening SQLite is insufficient.

Any of the following is an immediate HARD FAIL:

```text
SQLite opens but an authoritative relationship is broken
CAS reference silently lost
artifact hash mismatch undetected
snapshot path escape
restore overwrites an existing authoritative destination
candidate identity cannot be determined
post-restore runtime/job/workspace state wrongly treated as RUNNING/healthy
off-host backup without access-control/encryption governance record
```
