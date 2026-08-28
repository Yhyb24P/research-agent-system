# DQ06 — Production Go/No-Go Decision

DQ06 is a decision gate, not another feature milestone. It may be marked
**GO** only when every applicable DQ gate has evidence tied to the same RC,
target-environment manifest, and qualification date.

## Decision record

| Gate | Evidence required | Current status |
|---|---|---|
| DQ00 release/supply chain | immutable RC tag, retained release manifest, lock/SBOM and image digests | **PARTIAL** — RC is tagged; target evidence and SBOM/image records remain |
| DQ01 host/sandbox/filesystem | strict preflight plus target-host boundary, quota, cancellation and race evidence | **PARTIAL** — WSL2 evidence exists; deployment qualification remains pending |
| DQ02 GPU/backend | only if GPU backend is selected; scheduler-enforced device isolation and crash recovery | **NOT APPLICABLE** for CPU/cloud deployment; otherwise pending |
| DQ03 cloud | staging canaries, provider retry/cost/retention records, model-drift review | **PARTIAL** — deterministic tests pass; real provider evidence remains |
| DQ04 backup/DR | encrypted off-host snapshot, clean-host restore, RPO/RTO measurement and health report | **PARTIAL** — consistency tests pass; operational certificate remains |
| DQ05 operations/soak | mixed workload, fault/restart/reconcile evidence, thresholds and retained probe output | **PENDING** — probe is available; target run has not occurred |

## Decision rules

- **GO** requires all applicable rows to be GREEN and every exception to have
  an explicit owner, expiry, and risk acceptance. `PARTIAL`, `PENDING`, or an
  unmeasured threshold is not green.
- **CONDITIONAL GO** is allowed only for a narrowly scoped deployment (for
  example CPU-only local control plane without GPU) when excluded capabilities
  are recorded as unavailable and traffic is technically prevented from using
  them.
- **NO-GO** is mandatory for any failed safety invariant, missing restore
  evidence, unbounded provider behavior, unknown release provenance, or a
  soak threshold violation.

## Current decision

The current repository state is **CONDITIONAL GO for development/staging
evaluation only; NOT production GO**. The CPU/cloud path does not require GPU
resources. Production release remains blocked on target-environment DQ01,
DQ03, DQ04, and DQ05 evidence, not on GPU capacity.

The decision owner must attach the tagged RC, `release-manifest.json`,
`dq05-storage-evidence.json`, provider canary report, and clean-host restore
report before changing this status.

Before review, the retained JSON can be checked for provenance and explicit
pass status with:

```bash
.venv/bin/python scripts/dq06_evidence_check.py \
  --manifest release-manifest.json \
  --preflight-evidence dq01-preflight-evidence.json \
  --storage-evidence dq05-storage-evidence.json \
  --dr-evidence dq04-dr-evidence.json \
  --cloud-evidence dq03-cloud-evidence.json \
  --output dq06-evidence-check.json
```

The checker does not manufacture missing deployment evidence: absent optional
reports remain outside its scope and must still be attached to the decision.
