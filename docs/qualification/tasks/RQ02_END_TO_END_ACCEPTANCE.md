# RQ02 — End-to-End Release Acceptance

## Objective

Run one bounded, representative research workflow through the intended deployment topology and verify the complete evidence/control chain.

## Reference flow

```text
Human goal
  -> Cloud/lead plan
  -> WorkOrder
  -> policy decision
  -> optional exact-scope approval
  -> Delegation / AgentInvocation
  -> optional WorkspaceGrant
  -> execution / long Job
  -> Artifact + Observation
  -> independent VerificationResult
  -> Cloud/lead ReviewDecision
  -> final ResearchRun outcome
  -> AG-UI/researchctl observable timeline
```

## Required assertions

- authoritative state can be reconstructed from SQLite/audit records;
- every external/runtime action maps back to canonical Run/WorkOrder/Attempt/Invocation identities;
- artifact provenance is complete;
- verifier result is independent of executor narrative;
- approval is exact-scope and cannot be reused beyond contract;
- cloud context contains only allowed data;
- restart at one planned point preserves progress/recovery;
- user-visible event stream can resume from cursor;
- final result and unresolved limitations are explicit.

## Exit criteria

The reference workflow completes or fails in an expected controlled way with no hard invariant violation. A successful scientific result is not required; a trustworthy negative/failed workflow is acceptable if the control/evidence chain is correct.
