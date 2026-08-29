# DQ04 — Backup, Restore and Disaster Recovery Qualification

## Objective

Prove that authoritative SQLite state plus CAS artifacts can be backed up, transferred/stored according to policy, restored to a clean target and validated without silent divergence.

## Required scenarios

- online backup while controller is idle;
- online backup while writes are occurring;
- CAS reference inventory and missing/orphan detection;
- checksum validation;
- deliberately corrupted database backup rejected;
- deliberately corrupted CAS object rejected/detected;
- restore onto a clean host/path;
- restore after simulated primary loss;
- restored Run/WorkOrder/Attempt/Approval/Artifact/Audit relationships validated;
- post-restore controller startup/recovery;
- off-host storage access control and encryption/key-management procedure recorded;
- backup retention/rotation and deletion policy exercised at least once.

## Required metrics

Backup duration, backup size, RPO achieved in the drill, restore duration/RTO, validation duration and number of missing/orphan/corrupt references.

## HARD acceptance

A restore is not successful until database schema/version, CAS references, artifact hashes and authoritative relationships pass integrity checks. Merely opening SQLite is insufficient.
