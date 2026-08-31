# QF01 — Productization Impact and Requalification Matrix

## Scope and decision authority

This is change-control for the next candidate after the immutable historical
`v1.0.0-rc.80` line. It does not alter historical evidence and it does not
grant any Gate a PASS. The next candidate is not yet frozen; substitute its
tag, commit, environment fingerprint, and newly produced evidence IDs only in
a separately reviewed acceptance record.

Historical evidence references below are audit locators, not reuse claims.
The retained rc.79 matrix records predecessor IDs such as
`qe_rc79_iq01_real_interoperability` and `qe_rc79_dq01_host`; the rc.80
bundle itself is outside this source baseline and must be located by the
independent reviewer before any reuse decision. No historical object may be
edited or relabelled.

| Gate | Historical candidate / evidence locator | Productization changes and affected invariants | Environment dependency | Decision | Required rerun | Reviewer / acceptance status |
| --- | --- | --- | --- | --- | --- | --- |
| IQ01 | rc.80; retained IQ01 bundle, ID to be recovered by reviewer | A2A governed adapter, attachment leases, selector/invocation persistence and exact typed routing changed. Ownership, tenant/scope and duplicate-dispatch invariants are affected. | A2A peer and transport topology | `FULL_REQUALIFICATION` | Run IQ01 real interoperability matrix on the frozen new candidate. | Independent interoperability reviewer; PENDING. |
| IQ02 | rc.80; retained IQ02 bundle, ID to be recovered by reviewer | Trusted `workspace_sources`, automatic grant provisioning, transport lifecycle and reconciliation changed. Workspace isolation and bounded-path invariants are affected. | Git/workspace source, filesystem and transport behavior | `FULL_REQUALIFICATION` | Run IQ02 workspace transport/fault matrix on candidate. | Independent workspace reviewer; PENDING. |
| IQ03 | rc.80; retained IQ03 bundle, ID to be recovered by reviewer | Browser/SSE/TUI projections, cursor recovery, redaction and pane reconciliation changed. Read-model, cursor and no-secret invariants are affected. | Browser, loopback HTTP and SSE behavior | `RERUN_SOFTWARE_MATRIX` | Run IQ03 projection/reconnect/redaction matrix on candidate. | Independent projection reviewer; PENDING. |
| DQ01 | rc.80; retained DQ01 bundle, ID to be recovered by reviewer | Bubblewrap execution, `/workspace` host prerequisite, installed-wheel workflow and Workspace grants changed. Sandbox boundary and filesystem admission are affected. | Intended host, namespace policy, `/workspace`, bwrap/prlimit | `RERUN_DEPLOYMENT_EVIDENCE` | Reproduce DQ01 host/sandbox/filesystem probes in intended deployment. | Independent deployment reviewer; PENDING. |
| DQ02 | rc.80; retained DQ02 bundle, ID to be recovered by reviewer | RuntimeSession start identity, local runtime leases, heartbeat, restart reconciliation and managed invocation changed. Single-owner and no-stale-runtime invariants are affected. | Process supervisor and local Agent runtimes | `FULL_REQUALIFICATION` | Run DQ02 runtime lifecycle matrix on candidate. | Independent runtime reviewer; PENDING. |
| DQ03 | rc.80; retained DQ03 bundle, ID to be recovered by reviewer | Egress policy core remains fail-closed, but context/adapter/candidate workflow changed and routing must be reassessed. No reuse is asserted without review. | Provider credentials, network and trust zones | `RERUN_SOFTWARE_MATRIX` | Run DQ03 provider/egress matrix; add deployment evidence if route/config differs. | Independent egress reviewer; PENDING. |
| DQ04 | rc.80; retained DQ04 bundle, ID to be recovered by reviewer | Schema advanced `0020`–`0025`; runtime sessions, grants, invocations, claims and backup semantics changed. Restore integrity and candidate binding are affected. | SQLite/CAS storage and backup destination | `FULL_REQUALIFICATION` | Run DQ04 backup/restore/DR matrix on candidate schema. | Independent storage reviewer; PENDING. |
| DQ05 | rc.80; no inheritable soak claim | Orchestrator driver, approval transaction, runtime/Workspace fault paths and restart recovery changed. Endurance evidence is candidate and environment specific. | Intended deployment workload and fault injectors | `RERUN_DEPLOYMENT_EVIDENCE` | After relevant DQ01–DQ04 pass, run required soak/fault matrix. | Independent operational reviewer; PENDING. |
| RQ01 | rc.80; candidate-specific provenance record | Candidate tag, exact workflow, wheel/sdist, SBOM, manifest and accepted Gate IDs necessarily differ. | CI and artifact retention | `FULL_REQUALIFICATION` | Produce a new RQ01 record bound to the new immutable tag/commit. | Release reviewer distinct from producers; PENDING. |
| RQ02 | rc.80; candidate-specific release E2E record | Product workflow, Workspace/claims/verifier path and intended topology changed. | Intended topology, approval and Agent services | `FULL_REQUALIFICATION` | Run RQ02 end-to-end acceptance on candidate. | Independent release E2E reviewer; PENDING. |
| RQ03 | rc.80; candidate-specific GO/NO_GO record | GO depends on all newly accepted evidence, provenance/signing decision and release notes. | Release authority | `FULL_REQUALIFICATION` | Produce explicit GO or NO_GO only after RQ01/RQ02 and required IQ/DQ evidence. | Human release authority; NOT_STARTED. |

## Cross-cutting component map

| Changed area | Commits/range to assess | Gates necessarily affected |
| --- | --- | --- |
| Daemon orchestration and approval transactions | product-hardening after rc.80 | DQ02, DQ05, RQ01–RQ03 |
| RuntimeSession, local leases and managed Agent lifecycle | `9e646a9` and dependent productization commits | IQ01, DQ02, DQ05, RQ02 |
| Workspace sources/grants/transports | `5fd58af` and dependent commits | IQ02, DQ01, DQ05, RQ02 |
| Executor claims and verifier evidence path | `6066aba`, `48fa34a` and dependent commits | DQ04, DQ05, RQ02, RQ03 |
| Schema migrations `0020`–`0025` | product-hardening migration range | DQ04, RQ01–RQ03 |
| Browser/SSE/TUI and local control surface | PX/PH productization range | IQ03, RQ02 |
| Candidate workflow and artifact metadata | CR01 onward | RQ01, RQ03 |

## Reuse rule

`UNCHANGED_AND_REUSABLE` is not selected for any Gate above. A later reviewer
may propose it only in a new acceptance record that names the immutable
historical evidence ID, compares relevant code/config/environment fingerprints,
and is signed by an authority distinct from the evidence producer. A missing
locator or acceptance is a hard failure, not an implicit inheritance.
