# Qualification Gate Policy

## Gate states

```text
NOT_STARTED
IN_PROGRESS
BLOCKED
PASSED
FAILED
INVALIDATED
WAIVED
```

`WAIVED` is forbidden for hard safety/security requirements. A waiver is permitted only for explicitly marked non-hard checks and must contain owner, rationale, expiry and affected release claim.

## Check severity

- `HARD`: failure prevents Gate pass.
- `MAJOR`: failure prevents Production Go unless explicitly fixed and re-run.
- `ADVISORY`: recorded for follow-up but does not block the Gate.

## Evidence rules

A result counts only if all of the following are true:

1. exact `candidate_commit` is recorded;
2. the tested artifact/environment can be reconstructed from recorded fingerprints;
3. command/config/input/output identifiers are preserved;
4. expected result and observed result are both recorded;
5. logs are immutable or hashed;
6. any generated artifact has a content hash;
7. failures and retries are preserved, not overwritten;
8. the actor producing a result cannot also self-approve the Gate when the Gate concerns its own authority boundary.

## Gate invalidation

A Gate becomes `INVALIDATED` when a later change can materially affect its result. The invalidation event must point to the old evidence, change commit, affected checks and required reruns.

## Hard failure examples

The following always block the relevant Gate:

- unauthorized state transition;
- agent/runtime grants itself a trusted capability;
- verification result can be forged by executor output alone;
- LOCAL_ONLY or SECRET data crosses an external-cloud boundary;
- workspace path/symlink escape;
- expired grant remains usable;
- duplicate dispatch causes duplicate authoritative side effects where the operation contract requires deduplication;
- restart loses or corrupts authoritative workflow state;
- restore succeeds syntactically but CAS/database integrity is inconsistent;
- candidate commit/tag/evidence mismatch;
- test evidence is not attributable to the candidate under review.

## Acceptance record

Each Gate ends with a signed or otherwise attributable acceptance record containing:

```text
acceptance_id
gate_id
candidate_commit
candidate_tag
result
hard_failures
major_findings
accepted_evidence_ids
reviewer
reviewed_at
notes
```

The acceptance record is append-only. Corrections create a superseding record.
It must validate against `schemas/qualification_acceptance.schema.json` and be
checked together with the plan and referenced evidence by
`scripts/qualification_validate.py`.
