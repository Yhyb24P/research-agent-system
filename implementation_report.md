# ACC/0.1 Rust-v2 Implementation Report

## 1. Machine-readable summary
```json
{"report_version":"4","status":"PARTIAL","active_product":"rust-v2","acc_implementation_language":"rust","governance_resolution":"RESOLVED_BY_RUST_V2_SUPERSESSION","legacy_python_status":"PRE_EXISTING_LEGACY_FAILURE","core_tested_commit":"e23ae81759fc278fa02a5899ad7c6d03318d2848","phase23_base_commit":"e23ae81759fc278fa02a5899ad7c6d03318d2848","phase23_source_state":"DIRTY_R5_R6_PHASE23_WORKTREE","branch":"v2/rust-agent-team","core_claim":"CORE_ACC_READY","phase23_claim":"NOT_READY_FOR_GATE_K","adapter_claims":{"a2a":"NOT_READY","codex":"NOT_READY","claude_code":"NOT_READY","openclaw":"NOT_READY","qwen":"NOT_READY"},"generated_at":"2026-09-11T14:12:54+08:00"}
```

## 2. Current Git / branch / dirty state
- Core candidate HEAD `e23ae81759fc278fa02a5899ad7c6d03318d2848`; branch `v2/rust-agent-team`. It remains the qualified Core baseline, not the Phase 2.3 source candidate.
- Phase 2.3 runs on the actual dirty R5/R6 worktree. Tracked implementation paths are `Cargo.lock`, `crates/agent-code-runtime/{Cargo.toml,src/lib.rs}`, `crates/agent-code-storage/src/{board,lib,schema}.rs`, `crates/agent-code-storage/tests/acc_migration.rs`, and this report. Untracked implementation paths are `crates/agent-code-runtime/src/{codex_app_server.rs,bin/ras_codex_mcp.rs}`, `crates/agent-code-runtime/tests/codex_live.rs`, and `crates/agent-code-storage/tests/phase21_durability.rs`; evidence paths are listed in Section 24. `git diff --check` passed after Phase 2.3.
- Candidate implementation paths: `Cargo.lock`, `Cargo.toml`, `crates/agent-code-cli/{Cargo.toml,src/lib.rs}`, `crates/agent-code-storage/{Cargo.toml,src/lib.rs,src/schema.rs,src/acc_store.rs,tests/acc_e2e.rs,tests/acc_migration.rs}`, and `crates/agent-code-team/{Cargo.toml,src/lib.rs,src/acc.rs}`. The candidate also includes `implementation_report.md` and the prior `.acc-evidence/` history.

## 3. Rust workspace inventory
Workspace crates are `agent-code-{core,model,tools,workspace,context,storage,team,runtime,tui,cli}` plus integration/e2e packages. ACC uses `agent-code-team` for contracts/broker, existing SQLite storage for canonical state, and `agent-code-cli` for inspection. `SCHEMA_VERSION=7`; no second canonical task/event store exists.

## 4. Governance supersession resolution
The delivery pack's Python control-plane target conflicts with active Rust v2 in `AGENTS.md` (SHA-256 `5b9ccd8f21ac6d1b4be10ce04bff73ecf412831d1ba8ffbc1bf7ae2e80386565`). The user-authorized overlay resolves this as `RESOLVED_BY_RUST_V2_SUPERSESSION`: Python-specific paths/tooling/persistence are superseded, while ACC authority, review, artifact, provenance, and evidence invariants remain mandatory. No Python product code changed.

## 5. ACC responsibility mapping
| Responsibility | Rust location | Evidence |
|---|---|---|
| Contracts, DAG, context, broker, assignment, runtime/tools/acceptance | `crates/agent-code-team/src/acc.rs` | unit/full tests |
| Durable projections and recovery | `crates/agent-code-storage/src/{schema,acc_store}.rs` | migration/E2E |
| Four-identity deterministic loop | `crates/agent-code-storage/tests/acc_e2e.rs` | full test |
| Persisted-state inspection | `crates/agent-code-cli/src/lib.rs` | inspection test |

## 6. Implemented TASK status
| Item | Status | Evidence |
|---|---|---|
| Schema drift | COMPLETE | generated-schema SHA sentinel |
| Existing-journal migration/recovery | COMPLETE | pre-ACC v4 SQLite fixture |
| Runtime negotiation | COMPLETE | supported/unsupported input test |
| Artifact mismatch fail-closed | COMPLETE | mismatch/audit test |
| Canonical inspection | COMPLETE | `inspect_acc` persisted reads |

## 7. File change manifest
| Path | Action | Purpose |
|---|---|---|
| `Cargo.toml`, `Cargo.lock` | modified | schema dependency |
| `crates/agent-code-team/{Cargo.toml,src/lib.rs,src/acc.rs}` | modified/new | ACC contract/broker |
| `crates/agent-code-storage/{Cargo.toml,src/lib.rs,src/schema.rs,src/acc_store.rs}` | modified/new | v6 ACC store, then v7 runtime-binding seam |
| `crates/agent-code-storage/tests/{acc_e2e.rs,acc_migration.rs}` | new | E2E/migration |
| `crates/agent-code-cli/{Cargo.toml,src/lib.rs}` | modified | inspection API |
| `crates/agent-code-runtime/{Cargo.toml,src/lib.rs,src/codex_app_server.rs,src/bin/ras_codex_mcp.rs,tests/codex_live.rs}` | modified/new | bounded real Codex app-server/MCP bridge and live harness |
| `.acc-evidence/closeout-*.log`, this report | new | evidence/report |

## 8. Persistence / migration status
`SqliteAccStore` extends the existing journal with `acc_tasks`, `acc_dependencies`, `acc_context_manifests`, immutable hash/version `acc_artifacts`, and append-only sequenced/unique `acc_events`. Current v7 additionally has external runtime bindings and bounded collaboration records. The fixture creates an actual v4 journal with rows, migrates/reopens it, and verifies old rows plus ACC graph/context/event state. No Alembic was introduced.

## 9. ACC contract/schema status
Rust strict `serde` types are source-of-truth; unknown control fields are rejected. `schemars` generates `AccWireContractBundle`, whose deterministic hash is checked against `ACC_WIRE_SCHEMA_SHA256`. Capability, trusted capability, role, and authority remain distinct.

## 10. Runtime adapter status
`RuntimeAdapter`/`RuntimeDescriptor` are transport-neutral. `send_runtime_input` checks `midrun_input`: support invokes the adapter; lack of support returns explicit unsupported without invocation. Phase 2.3 adds a narrow real Codex app-server stdio client plus an allowlisted RAS MCP bridge; its live evidence is recorded in Section 24. This does not satisfy the complete existing Gate K reference qualification.

## 11. Exact test commands and results
| Command | Exit | Result | Log |
|---|---:|---|---|
| `cargo fmt --all -- --check` | 0 | passed | `.acc-evidence/candidate-fmt.log` |
| `cargo clippy --workspace --all-targets --all-features -- -D warnings` | 0 | passed | `.acc-evidence/candidate-clippy.log` |
| `cargo test --workspace --all-features` | 0 | 134 passed, 0 failed | `.acc-evidence/candidate-test.log` |
| `cargo test -p agent-code-team generated_wire_schema -- --nocapture` | 0 | 1 passed | `.acc-evidence/candidate-schema.log` |
| `cargo test -p agent-code-storage --test acc_migration` | 0 | 1 passed | `.acc-evidence/candidate-migration.log` |
| `cargo test -p agent-code-cli` | 0 | 1 passed | `.acc-evidence/candidate-inspection.log` |
| `git diff --check` | 0 | passed | `.acc-evidence/candidate-diff-check.log` |
The earlier 128-test result remains historical evidence; 134 is the exact-candidate closeout run.

## 12. Gate A–N current status
| Gate | State | Evidence |
|---|---|---|
| A | PASSED | AGENTS/git recorded; Rust gates exit 0 |
| B | PASSED | strict types/separation/schema drift |
| C | PASSED | v6 constraints/migration/recovery |
| D | PASSED | DAG/readiness/assignment tests |
| E | PASSED | typed broker/binding/dedupe/order |
| F | PASSED | provenance/hash/redaction |
| G | PASSED | tools/negotiated runtime operation |
| H | PASSED | persisted four-agent E2E |
| I | PASSED | review/replay/restart/privacy/mismatch tests |
| J | NOT_RUN | no A2A live evidence |
| K | NOT_RUN | Phase 2.3 has narrow Codex live bridge evidence, but not the established full Gate K qualification |
| L | NOT_RUN | no Claude Code live evidence |
| M | NOT_RUN | no OpenClaw live evidence |
| N | PASSED | full/targeted regression and report |

## 13. Security / authority assertions
- Trusted grants are distinct from agent capability; runtime-bound actor/authority is normalized.
- `RESULT_SUBMITTED != ACCEPTED`; independent review rejects executor self-review.
- Artifact hash/version mismatch is rejected, audited, and leaves canonical acceptance unchanged.
- Prohibited private/secret material is excluded from context; event IDs dedupe and SQLite restart recovers canonical history.

## 14. Legacy Python status
`PRE_EXISTING_LEGACY_FAILURE`. Python sources, Alembic, and Python tests were not changed or re-run. Rust results do not claim that legacy Python is green or repaired.

## 15. Qualification impact
Frozen Python qualification artifacts remain unchanged. Rust impact is the SQLite v6 ACC extension and the current executable evidence; the migration fixture qualifies existing Rust journal compatibility only.

## 16. Deviations
Python paths, Pydantic, SQLAlchemy/Alembic, pytest, and mypy implementation mandates are superseded by the overlay. Inspection is existing CLI-crate read-only API `agent_code_cli::inspect_acc`, not parallel Python `researchctl`.

## 17. Outstanding blockers
| ID | State | Blocks | Required action |
|---|---|---|---|
| LIVE-A2A | NOT_RUN | `A2A_REFERENCE_READY` | real observable A2A probe |
| LIVE-CODEX | NOT_RUN | `CODEX_REFERENCE_READY` | real Codex probe |
| LIVE-CLAUDE | NOT_RUN | `CLAUDE_REFERENCE_READY` | real Claude Code probe |
| LIVE-OPENCLAW | NOT_RUN | `OPENCLAW_REFERENCE_READY` | real OpenClaw probe |
They do not block core under Gate 04; they block `FULL_REFERENCE_READY`.

## 18. Reproduction commands
```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test -p agent-code-team generated_wire_schema -- --nocapture
cargo test -p agent-code-storage --test acc_migration
cargo test -p agent-code-cli
cargo test --workspace --all-features
git diff --check
```

## 19. Evidence manifest

Every row below is bound to source `git:e23ae81759fc278fa02a5899ad7c6d03318d2848`, exit 0, and the observed result in Section 11. `executed_at` is the recorded file completion time (+08:00).

| File | SHA-256 | executed_at |
|---|---|---|
| `.acc-evidence/candidate-fmt.log` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | `2026-09-11T08:02:54+08:00` |
| `.acc-evidence/candidate-clippy.log` | `59639588a6f1826f129cbcd121edc07279dab94786f5fa5bde6b3439b2e845cc` | `2026-09-11T08:02:56+08:00` |
| `.acc-evidence/candidate-schema.log` | `673eb792fe12ddef0c77486382b433904e4bc378bf9b7e7489197aca7f6a2783` | `2026-09-11T08:02:58+08:00` |
| `.acc-evidence/candidate-migration.log` | `3fa743a5303ed908d9637619357151bff0a3a759a5fe534be2b116c23fab37f1` | `2026-09-11T08:03:03+08:00` |
| `.acc-evidence/candidate-inspection.log` | `130908f2bd648fe8be5800e6e94333e5076f50c112607488caaad78b7bd4e619` | `2026-09-11T08:03:05+08:00` |
| `.acc-evidence/candidate-test.log` | `0414c0933c5e431b5017692d1a036e1281e03ea8eebf979bcd38e028aedbbdd6` | `2026-09-11T08:03:15+08:00` |
| `.acc-evidence/candidate-diff-check.log` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | `2026-09-11T08:03:17+08:00` |

Additional bound inputs: candidate report Git blob `f12eb1be0e266a1af5e5f4f629404eda9be95621`; ACC implementation blob `b9a63e2bbe22723274542e0f385f5e0b86e9f789`; schema sentinel `19cee109cd16d036fd63b7726e076cc795fbd22413de284050576dadb4ffe53c`; migration fixture `crates/agent-code-storage/tests/acc_migration.rs` in the candidate tree. The final report's separately computed SHA-256 is supplied in the handoff because a document cannot contain a stable hash of its own final bytes.

## 20. Allowed claims
- `CORE_ACC_READY`: Rust-v2 Gates A–I and N are PASSED with commands, exits, observations, and hashes above.
- Active product is Rust v2; ACC implementation language is Rust.

## 21. Forbidden claims
- Do not claim `FULL_REFERENCE_READY` or any adapter READY claim.
- Do not claim legacy Python is repaired, green, or revalidated.
- Do not treat deterministic fixtures as live adapter evidence.

## 22. Phase 2 reference-adapter probe status

```yaml
phase: reference-adapters
core_baseline_commit: e23ae81759fc278fa02a5899ad7c6d03318d2848
core_freeze_status: PRESERVED
core_files_modified: []
cross_adapter_e2e: NOT_RUN
reference_adapters:
  codex:
    status: PROBED_NOT_INTEGRATED
    executable: codex (resolved locally through PATH)
    exact_version: codex-cli 0.154.0
    integration_path: app-server stdio JSON-RPC-like protocol
    protocol_schema_sha256: d71ddf3bf5484f8de2799f7a4793c2e66808a9ec1a330e2307accb088ab5948a
    observed: [app-server available, schema generation, thread/turn/resume/interrupt protocol types]
    gate_k: NOT_RUN
  claude_code:
    exact_version: 2.1.220
    status: NOT_RUN
    gate_l: NOT_RUN
  openclaw:
    status: BLOCKED
    blocker: "requires Node >=24.15.0; observed 24.14.0"
    gate_m: NOT_RUN
  a2a:
    status: NOT_RUN
    gate_j: NOT_RUN
```

These are local executable probes, not active ACC collaboration evidence. No
adapter implementation or Core change was made in this probe-only step.

## 23. Phase 2.1 / 2.2 R6 durable runtime seam repair

Product alignment follows current `AGENTS.md` and `docs/v2/ROADMAP.md`: this is
an R6 heterogeneous coding/work-team integration seam, not a Trusted Control
Plane change. `R6_INTEGRATION_SEAM_CHANGED` affected
`agent-code-storage`; product semantics changed: **NO**.

| Observed gap / failing-test intent | Existing path | Minimal repair | Result |
|---|---|---|---|
| no durable task/run → external native handle | `SqliteTaskBoard` / `team_task_runs` | v7 `external_runtime_bindings`, keyed by canonical team task + attempt | passed after reopen |
| no recoverable runtime help/context record | existing board message/artifact domain | v7 bounded `runtime_collaboration_records` | passed after reopen |
| repeated native call could be replayed after crash | no idempotency key in board | unique `(runtime_kind,native_call_id)` and `DO NOTHING` | second insert returns false |
| restart could not load active external reference | board had no runtime binding read API | `external_binding(task,attempt)` recovery query | binding restored |

The initial targeted implementation compile exposed Rust error `E0597` in the
query iterator lifetime; the safe local repair binds `MappedRows` before
collecting. No runtime protocol or product invariant changed.

Failing-tests-first evidence was run in an isolated detached worktree at
candidate `e23ae81759fc278fa02a5899ad7c6d03318d2848`, then removed. The
temporary `phase21_missing_seam` test ran
`cargo test -p agent-code-storage --test phase21_missing_seam` with exit
`101`: `ExternalRuntimeBinding` was unresolved and
`SqliteTaskBoard::upsert_external_binding` did not exist. That failure maps to
the prior R5 board call chain (`SqliteTaskBoard` → `TaskBoard` task/run/message/
artifact persistence); the minimal repair is the v7 binding table/API above,
not a second store. Its successor test is
`phase21_durability::external_binding_and_idempotent_collaboration_survive_reopen`
and passes on the repaired worktree.

Storage migration: local version `6 → 7`. Existing schema migration remains
append-only; fresh creation and v4→current migration run in the workspace test
suite, and the new reopen test covers the R6 records. Only compact summaries,
native external references, and lifecycle are persisted: no credentials,
system prompts, hidden reasoning, or raw private transcript.

| Verification | Exit | Observed |
|---|---:|---|
| `cargo test -p agent-code-storage --test phase21_durability` | 0 | 1 passed: binding, idempotent collaboration, reopen |
| `cargo clippy --workspace --all-targets -- -D warnings` | 0 | passed |
| `cargo fmt --all -- --check` | 0 | passed |
| `cargo test --workspace` | 0 | 135 passed, 0 failed |
| `git diff --check` | 0 | passed |

This is not live Codex evidence. Gates J/K/L/M/Q and cross-runtime E2E remain
`NOT_RUN`; `PHASE2_ACTIVE_PROFILE_READY` and `FULL_REFERENCE_READY` remain
false. The next authorized phase is the real Codex app-server bridge; Qwen is
not started by this repair.

Phase 2.2 source state: base HEAD
`e23ae81759fc278fa02a5899ad7c6d03318d2848`; current uncommitted seam paths
are `crates/agent-code-storage/src/{board,lib,schema}.rs`,
`crates/agent-code-storage/tests/{acc_migration,phase21_durability}.rs`, and
this report. The historical candidate evidence logs remain untracked and are
not part of this seam change. Current local facts: `AGENTS.md` SHA-256
`5b9ccd8f21ac6d1b4be10ce04bff73ecf412831d1ba8ffbc1bf7ae2e80386565`;
`docs/v2/ROADMAP.md` SHA-256
`ab24d2c5c8f5ac286755d1ebfba031f064e0b50f9eb6d66d68461986d8fe29f4`.

## 24. Phase 2.3 — real Codex app-server active collaboration bridge

### Source and wire facts

The Phase 2.3 source candidate is the actual R5/R6 dirty worktree on base
`e23ae81759fc278fa02a5899ad7c6d03318d2848`, not that baseline commit alone.
Its bridge paths are `crates/agent-code-runtime/src/codex_app_server.rs`,
`crates/agent-code-runtime/src/bin/ras_codex_mcp.rs`, and
`crates/agent-code-runtime/tests/codex_live.rs`; related durable seam paths
are listed in Section 2. No Qwen, Claude Code, OpenClaw, or A2A path was added.

Local executable: `codex` (resolved through local PATH); observed version:
`codex-cli 0.154.0`. Local app-server schema was generated with
`codex app-server generate-json-schema --out <temporary directory>` and has
SHA-256 `d71ddf3bf5484f8de2799f7a4793c2e66808a9ec1a330e2307accb088ab5948a`.
The live child used `codex app-server --stdio` with only per-process
`mcp_servers.ras.*` overrides; no persistent user Codex configuration changed.

The local schema was the wire truth. It confirms initialize/initialized,
thread/start, turn/start, item/tool/call, turn/completed, turn/interrupt, and
the dynamic-tool result shape. Contrary to the delivery wording, installed
`ThreadStartParams` has no `dynamicTools` property. The bridge does not invent
it: it uses the locally probed MCP integration and only
`mcpServer/elicitation/request` for named RAS bridge calls.

### Implemented bounded bridge and live result

`CodexAppServer` is a stdio JSON-RPC client. `ras_codex_mcp` exposes exactly
`ras_request_context` and `ras_request_help`; it has no acceptance, admin,
shell, generic approval, or unrestricted-context tool. Only literal server
name `ras` can receive an elicitation response. MCP JSON-RPC request id, not
tool arguments, is the idempotency key. The bridge persists a compact
maximum-512-character summary and fixed response summary; it never persists
credentials, prompts, hidden reasoning, raw private transcripts, or protocol
bodies. Native thread/turn ids remain external references. `turn/completed`
does not imply ACC acceptance.

The explicitly invoked ignored live harness created a real app-server thread
and turn, confirmed connected RAS MCP tools, caused Codex to emit
`mcpToolCall(ras_request_context)`, accepted only RAS elicitation, observed the
completed MCP call, then a later `agentMessage` in the same turn. It reopened
SQLite and verified the durable collaboration record and external binding.

| Milestone | State | Actual evidence |
|---|---|---|
| `C21_TRANSPORT_READY` | PASSED | live initialize/thread/turn stdio exchange |
| `C21_DURABILITY_READY` | PASSED | v7 reopen/idempotency and live reopen assertions |
| `C21_ACTIVE_BRIDGE_READY` | PASSED | Codex request → RAS response → same-turn continuation |
| `C21_TEAM_FLOW_READY` | IN_PROGRESS | no independently reviewed accepted artifact in this harness |
| `PHASE2_1_CODEX_READY` | false | not claimed |

### Phase 2.3 evidence

| Command | Exit | Observed result | Log SHA-256 | Executed at |
|---|---:|---|---|---|
| `cargo test -p agent-code-runtime --test codex_live -- --ignored --nocapture` | 0 | 1 live test passed | `5dbc86a2586c3032ff1d806c0932c75554872fdc1c0e0934ed4fa716ed42468f` | 2026-09-11T14:12:25+08:00 |
| `cargo test -p agent-code-storage --test phase21_durability` | 0 | 1 passed | `9aaf854fb0fb6dd66c3740231dd6821bc960c227556efab1492dbad9c4f90c10` | 2026-09-11T14:12:45+08:00 |
| `cargo fmt --all -- --check` | 0 | passed | `dac4aefe9211749d3d49520b005d5c0bc079e1b5f82e7d007889b04e35fa6034` | 2026-09-11T14:12:45+08:00 |
| `cargo clippy --workspace --all-targets -- -D warnings` | 0 | passed | `23ff7698e1abd214e04021727f5529c5014001f4bb41dd0898812f8cff8b5825` | 2026-09-11T14:12:45+08:00 |
| `cargo test --workspace` | 0 | 135 passed, 0 failed, 1 ignored | `a6e1108dc7a0372b9f8dc4922b6e6ee9b72b2613081e2c13239961800124a787` | 2026-09-11T14:12:46+08:00 |
| `git diff --check` | 0 | passed | `bc495e4046f01dca79a5273ed4df9d6d157edc2e8f9b2121a1c95494ef52f662` | 2026-09-11T14:12:54+08:00 |

The first Phase 2.3 clippy attempt found a real `clippy::useless_conversion`
and exited 101. The `.into()` was removed; only the fresh post-fix run above
is PASS evidence. Gate J, K, L, M, Q and cross-runtime E2E remain `NOT_RUN`.
The narrow live bridge does not make Gate K PASSED. No active-profile or full
reference readiness claim is made; Qwen work has not begun.
