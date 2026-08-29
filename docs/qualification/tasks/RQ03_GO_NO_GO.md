# RQ03 — Production Go / No-Go

## Objective

Produce the final deployment decision from accepted Gate evidence. This task does not generate missing evidence and cannot waive hard failures.

## Inputs

- RQ01 release provenance record;
- RQ02 end-to-end acceptance record;
- accepted IQ01-IQ03 evidence;
- accepted DQ01-DQ05 evidence;
- unresolved MAJOR/ADVISORY findings;
- documented scope boundaries and target deployment topology.

## Decision rules

`GO` requires:

- all HARD checks passed;
- no Gate is INVALIDATED or INCONCLUSIVE;
- target topology exactly matches the qualified topology or has an explicit delta assessment;
- operational owner knows backup/restore, cancellation, recovery and incident procedures;
- known limitations are preserved in release notes and are not contradicted by product claims.

`NO_GO` is mandatory when a HARD failure remains, evidence cannot be tied to the candidate, or the target deployment differs materially from the qualified environment without requalification.

## Decision record

```text
candidate_commit
candidate_tag
decision: GO | NO_GO
qualified_topology
accepted_gate_records
open_major_findings
open_advisories
hard_blockers
release_claims_allowed
release_claims_forbidden
decision_owner
decided_at
```

A later change never edits this record. It creates a new candidate and a new RQ03 decision.
