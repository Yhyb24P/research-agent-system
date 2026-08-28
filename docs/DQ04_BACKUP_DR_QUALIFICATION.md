# DQ04 — Backup, Restore, and Disaster-Recovery Qualification

## Consistency contract

The backup operation now takes a SQLite online snapshot first, then queries
that snapshot's `artifacts` table. Only the referenced content-addressed hashes
are copied from CAS. The manifest records checksums for exactly those files;
unreferenced CAS residue is intentionally excluded.

Restore verifies all of the following before copying to a destination:

- database checksum;
- every artifact checksum and absence of unexpected files;
- equality between database artifact references and manifest paths;
- destination paths do not already exist.

After copying, `check_restored_snapshot()` provides a read-only health check:
SQLite integrity and foreign-key checks, required table counts, schema revision,
and content-addressed artifact re-hashing.

This proves database/CAS referential consistency for the snapshot. It does not
yet provide encryption, off-host retention, key management, RPO/RTO guarantees,
or a clean-environment production restore certificate.

## Implemented evidence

- A regression test creates an unreferenced CAS file and verifies it is omitted.
- A regression test deletes a referenced CAS file and verifies backup refuses
  to create an incomplete snapshot.
- A copied database with a tampered artifact reference is rejected during
  restore even after its database checksum is rewritten.
- Existing corruption/tamper and restore tests remain active.
- The restore path refuses a manifest whose files do not match the database's
  referenced artifact set.

## Pending operational qualification

Define RPO/RTO, encrypt and transport snapshots off-host, perform a clean-host
restore using only the tagged RC and backup, then run controller health checks
and verify WorkOrder, Attempt, Artifact, Verification, Approval, and Audit
records. These are deployment operations, not conclusions from unit tests.

The repository includes a timing/evidence probe for the clean-host exercise:

```bash
.venv/bin/python scripts/dq04_dr_probe.py \
  --database /var/lib/researchd/orchestrator.db \
  --artifacts /var/lib/researchd/artifacts \
  --snapshot /var/lib/researchd/backups/dq04-rc20 \
  --restore-root /var/tmp/researchd-dq04-restore \
  --last-committed-at 2026-08-29T00:00:00Z \
  --output dq04-dr-evidence.json
```

It measures snapshot and restore+health-check duration and records RPO when a
trusted last-commit timestamp is supplied. Encryption, off-host transfer,
clean-host provenance, and the pass thresholds remain deployment obligations.
