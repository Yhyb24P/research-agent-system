# V1 Qualification Mainline Plan

## 1. Baseline

Planning baseline:

- candidate family: `v1.0.0-rc.*`
- planning base commit: `df461a601929be3bc4aed10190f9ac8a05664a61`
- software baseline: Agent collaboration core, A2A v1 boundary, bounded Workspace Delegation Plane, AG-UI/SSE projection, optional LangGraph specialist runtime, trusted control plane, verifier, policy, approvals, CAS artifact/provenance, backup helpers, metrics and CI gates.

This plan intentionally freezes major architecture expansion. New frameworks, public services, generalized distributed execution, and unrelated UI feature work are out of scope until qualification blockers are resolved.

## 2. Release claim ladder

Use these claims exactly:

| Level | Allowed claim |
|---|---|
| L0 | Software under active development |
| L1 | Software Gate complete |
| L2 | Interoperability qualified |
| L3 | Target deployment qualified |
| L4 | Release candidate qualified |
| L5 | Production Go approved |

A later claim implies all earlier Gates have evidence bound to the same candidate commit, or an explicitly documented requalification decision.

## 3. Ordering

```text
Freeze candidate
    |
    +--> IQ01 A2A ------------------+
    +--> IQ02 Workspace ------------+--> IQ Gate
    +--> IQ03 AG-UI ----------------+
                                    |
                                    v
                    +--> DQ01 Host/Sandbox
                    +--> DQ02 Agent Runtime/Lifecycle
                    +--> DQ03 Cloud/Egress
                    +--> DQ04 Backup/DR
                    +--> DQ05 Soak/Fault
                                    |
                                    v
                              Deployment Gate
                                    |
                         +----------+----------+
                         v                     v
                  RQ01 Provenance       RQ02 E2E Acceptance
                         +----------+----------+
                                    v
                              RQ03 Go/No-Go
```

IQ01-IQ03 may run in parallel after the candidate is frozen. DQ01-DQ04 may also run in parallel once their target environment configuration is recorded. DQ05 starts only after the relevant DQ01-DQ04 components pass their local hard gates.

## 4. Global invariants under qualification

Every Gate must prove that these invariants survive the tested condition:

```text
Cloud/remote Agents propose; they do not own workflow state.
Controller owns state transitions and policy decisions.
Verifier is independent from execution and review.
Human approval is exact-scope, parameter-bound and auditable.
Artifacts are immutable and provenance-bearing.
Workspace reconciliation is artifact-only.
Protocol/runtime identity never replaces ResearchRun/WorkOrder/Attempt identities.
LOCAL_ONLY and SECRET data never become cloud-visible through UI, logs, protocol adapters or provider retries.
Failure must close access or stop progress; it must not silently broaden authority.
```

## 5. Change control during qualification

A candidate-affecting code/config change invalidates evidence according to impact:

- trusted core, policy, verifier, storage schema, artifact semantics: invalidate all affected IQ/DQ/RQ evidence;
- adapter/runtime implementation: invalidate the corresponding IQ/DQ Gate plus downstream RQ;
- docs-only change: no requalification unless it changes acceptance semantics;
- target environment change: invalidate the affected DQ Gate and downstream RQ.

Every evidence bundle must record `candidate_commit`, `environment_fingerprint`, `started_at`, `completed_at`, `tool_versions`, `checks`, `artifacts`, and `result`.

## 6. Required outputs

The qualification program produces, outside the tracked source baseline unless explicitly promoted:

```text
qualification-evidence/
  <candidate_commit>/
    IQ01/
    IQ02/
    IQ03/
    DQ01/
    DQ02/
    DQ03/
    DQ04/
    DQ05/
    RQ01/
    RQ02/
    RQ03/
```

Raw evidence files are intentionally not source-controlled by default. The repository tracks schemas, procedures, tests and acceptance logic; the evidence store holds environment-specific results.

## 7. Completion definition

The mainline is complete only when RQ03 records a deterministic `GO` or `NO_GO` decision. `GO` requires all hard Gates to pass. `NO_GO` is a valid completion outcome and must enumerate blockers without rewriting historical evidence.
