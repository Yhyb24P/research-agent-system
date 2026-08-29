# QF00 — rc.79 Evidence Consolidation and Gate Impact Matrix

> Candidate: `v1.0.0-rc.79` → `256d775f95c974e9a87633c6ee60dec12381bd58`
> Status: DRAFT analysis. The decisions in §4 are proposals and take effect
> only when recorded in acceptance objects by a reviewer independent of the
> evidence producer (see `GATE_POLICY.md`: no self-acceptance).

## 1. Candidate identity

```text
candidate_tag    = v1.0.0-rc.79
candidate_commit = 256d775f95c974e9a87633c6ee60dec12381bd58
origin           = merge of PR #8 (dq03-provider-egress), 2026-08-29T20:48Z
evidence bundle  = qualification-evidence/256d775f95c974e9a87633c6ee60dec12381bd58/
```

## 2. Method

For each gate IQ01–IQ03 and DQ01–DQ03:

1. locate the current-candidate evidence object in the rc.79 bundle;
2. locate the most recent historical PASS evidence (bundles rc.72–rc.78);
3. compute the `src/` change surface between the historical evidence commit
   and the candidate commit;
4. record changed components, affected invariants and a requalification
   decision (`FULL_RERUN` / `PARTIAL_RERUN` / `EVIDENCE_REUSE`).

## 3. Finding: current evidence is on-candidate

All six current evidence objects were produced **on the candidate commit
itself**, minutes after the PR #8 merge (2026-08-29T20:50:53Z–20:51:23Z).
`git diff --name-only <evidence_commit> 256d775 -- src/` is therefore empty
for every gate in the current bundle.

| Gate | evidence_id | result | checks (PASS/FAIL) |
|---|---|---|---|
| IQ01 | qe_rc79_iq01_real_interoperability | PASS | 12/0 |
| IQ02 | qe_rc79_iq02_fault_matrix | PASS | 14/0 |
| IQ03 | qe_rc79_iq03_replay_matrix | PASS | 10/0 |
| DQ01 | qe_rc79_dq01_host | PASS | 10/0 |
| DQ02 | qe_rc79_dq02_runtime | PASS | 12/0 |
| DQ03 | qe_rc79_dq03_provider_egress | PASS | 12/0 |

Common fields: `candidate_commit` = 256d775…, `candidate_tag` = v1.0.0-rc.79,
`environment_fingerprint` =
`sha256:edd42bf1c0e7ac7285045c9cb8a6d4264c1d5be4005d28a8bfee165ad1102226`
(`environment/host-preflight.json`, SHA-256 re-verified),
producer = SYSTEM / `local-qualification-runner`,
`supersedes_evidence_id` = null for all six.

**No historical PASS is inherited into rc.79.** The historical bundles
(`ef4bb9b`/rc.72, `1fc23f1`/rc.73, `a4636d0`/rc.75, `aa14835`/rc.76,
`4eaae72`/rc.77, `6555522`/rc.78) are archival records only and must not be
used to qualify the current candidate. `1fc23f1`/rc.73 contains raw test
artifacts without structured evidence objects and never constituted valid
evidence.

## 4. Gate impact matrix

`previous_evidence_commit` = most recent historical PASS for the gate.
Change surface = `src/researchd/` subdirectories touched between that commit
and the candidate.

| Gate | previous evidence | change surface → rc.79 | affected invariants | decision |
|---|---|---|---|---|
| IQ01 | `6555522` (rc.78, 12/12) | storage, models, context, agents (PR #8) | none: A2A adapter untouched since rc.78 | FULL_RERUN — already satisfied by on-candidate evidence |
| IQ02 | `6555522` (rc.78, 14/14) | storage, models, context, agents | workspace transport untouched | FULL_RERUN — already satisfied |
| IQ03 | `6555522` (rc.78, 10/10) | storage, models, context, agents | AG-UI projection untouched | FULL_RERUN — already satisfied |
| DQ01 | `6555522` (rc.78, 10/10) | storage, models, context, agents | host/sandbox/executor untouched; environment fingerprint re-captured on rc.79 | FULL_RERUN — already satisfied |
| DQ02 | `6555522` (rc.78, 12/12) | storage, models, context, agents | runtime lease/invocation untouched; storage schema 0019 added by PR #8, covered by on-candidate rerun | FULL_RERUN — already satisfied |
| DQ03 | none (first evidence is rc.79) | n/a | n/a | FULL_RERUN — first production, on-candidate |

Because the rerun was executed on the candidate at evidence production time,
no `EVIDENCE_REUSE` decision is required for any gate. Earliest historical
PASS per gate (for audit trail only): IQ01 `4eaae72`/rc.77, IQ02/IQ03
`a4636d0`/rc.75, DQ01 `aa14835`/rc.76, DQ02 `6555522`/rc.78, DQ03 none.
`ef4bb9b`/rc.72 evidence was INCONCLUSIVE and is disregarded.

## 5. QF00-02 bundle conformity

Mapping of the required bundle fields to `schemas/qualification_evidence.schema.json`:

| required field | schema field | status |
|---|---|---|
| gate_id | `gate_id` | present |
| candidate_tag / candidate_commit | `candidate_tag` / `candidate_commit` | present, bound to rc.79 |
| environment_fingerprint | `environment_fingerprint` | present, artifact SHA-256 verified |
| producer_identity | `producer` (actor_type/actor_id) | present |
| reviewer_identity | not an evidence field; lives in acceptance `reviewer` | n/a — see §6 |
| started_at / completed_at | `started_at` / `completed_at` | present |
| tool_versions | `tool_versions` | present |
| checks / artifacts / result | `checks` / `artifacts` / `result` | present; artifact SHA-256 re-verified |
| supersedes | `supersedes_evidence_id` | present, null |

Evidence objects are immutable once produced; the null `supersedes` links are
**not** retrofitted. Supersession of the historical bundles is recorded by
this matrix instead.

## 6. QF00-03 reviewer independence

- Producer of all six evidence objects: SYSTEM / `local-qualification-runner`.
- Acceptance records in the evidence store: **zero**. All six gates remain
  `IN_PROGRESS` with empty `accepted_evidence_ids`.
- Required: acceptance per `schemas/qualification_acceptance.schema.json`
  from a reviewer identity distinct from `local-qualification-runner`
  (`reviewer.actor_type` HUMAN or SYSTEM, different `actor_id`).
- The producing agent must not self-accept. OPEN.

## 7. QF00-04 DQ03 production-intent provider evidence

The software test-provider matrix (`qe_rc79_dq03_provider_egress`) does not
satisfy DQ03 exit. External evidence against a production-intent
provider/account is still required, recording at minimum:

```text
provider, model, endpoint, account/project, region, timeout, retry,
privacy/retention/training settings, structured-output mode,
token accounting, cost accounting, egress policy, cancellation,
no implicit fallback
```

Secrets must not enter the evidence bundle. This requires operator-supplied
account configuration and cannot be produced from the repository alone.
OPEN — external dependency.

## 8. QF00 exit gate status

| requirement | status |
|---|---|
| IQ01–IQ03, DQ01–DQ03 valid on-candidate evidence | satisfied (6 × PASS, verified) |
| requalification decisions recorded | proposed in §4, pending reviewer acceptance |
| reviewer-independent acceptance (QF00-03) | OPEN |
| DQ03 production-intent provider evidence (QF00-04) | OPEN — external dependency |

QF00 does not block DQ04 development; it blocks entry into release
qualification (RQ01–RQ03).
