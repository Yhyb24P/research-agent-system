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
