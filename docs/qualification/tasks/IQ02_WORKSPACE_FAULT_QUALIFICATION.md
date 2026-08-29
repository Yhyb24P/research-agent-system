# IQ02 — Workspace Transport and Fault Qualification

## Objective

Prove that bounded Workspace Delegation remains contained under transport failure, malicious paths, lease expiry, restart and reconciliation faults.

## Required scenarios

| ID | Scenario | Severity |
|---|---|---|
| IQ02-01 | Git worktree happy path with artifact-only reconciliation | HARD |
| IQ02-02 | Archive happy path with artifact-only reconciliation | HARD |
| IQ02-03 | source manifest mismatch | HARD |
| IQ02-04 | forbidden path and excluded path access | HARD |
| IQ02-05 | symlink/path traversal escape attempt | HARD |
| IQ02-06 | max bytes/file count/single-file limits | HARD |
| IQ02-07 | lease expires before reconciliation | HARD |
| IQ02-08 | controller crash during provisioning | HARD |
| IQ02-09 | controller crash during reconciliation | HARD |
| IQ02-10 | transport disappears or returns corrupt archive | HARD |
| IQ02-11 | duplicate reconciliation request | HARD |
| IQ02-12 | cleanup failure preserves observable cleanup state | MAJOR |
| IQ02-13 | classification ceiling exceeds Agent trust zone | HARD |
| IQ02-14 | remote result cannot overwrite authoritative source workspace | HARD |

## Required assertions

Every failure must leave authoritative source data unchanged, preserve an auditable terminal/intermediate state, and prevent expired/invalid transport handles from silently continuing.

## Evidence

Record source and result manifests, workspace grant/lease state, transport handle hashes or identifiers, injected fault timing, artifact reconciliation hash, cleanup state and authoritative source-tree hash before/after each fault.

## Exit criteria

Zero path escapes, zero direct authoritative source overwrite, zero usable expired lease, and deterministic recovery/failure semantics for all HARD scenarios.
