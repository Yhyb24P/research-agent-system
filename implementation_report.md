# ACC/0.1 Rust-v2 Implementation Report

## 1. Machine-readable summary
```json
{"report_version":"2","status":"PARTIAL","active_product":"rust-v2","acc_implementation_language":"rust","governance_resolution":"RESOLVED_BY_RUST_V2_SUPERSESSION","legacy_python_status":"PRE_EXISTING_LEGACY_FAILURE","tested_commit":"f677ec1eb2967430f15b96ded4163e023bc0997b","branch":"v2/rust-agent-team","worktree_state":"DIRTY_ACC_IMPLEMENTATION","core_claim":"CORE_ACC_READY","adapter_claims":{"a2a":"NOT_READY","codex":"NOT_READY","claude_code":"NOT_READY","openclaw":"NOT_READY"},"generated_at":"2026-09-11T07:56:57+08:00"}
```

## 2. Current Git / branch / dirty state
- HEAD `f677ec1eb2967430f15b96ded4163e023bc0997b`; branch `v2/rust-agent-team`.
- Dirty ACC worktree: root Cargo files; CLI/storage/team sources; new ACC sources/tests; `.acc-evidence/`; and this report. This evidence is for the dirty worktree, not a release tag. `git diff --check` passed.

## 3. Rust workspace inventory
Workspace crates are `agent-code-{core,model,tools,workspace,context,storage,team,runtime,tui,cli}` plus integration/e2e packages. ACC uses `agent-code-team` for contracts/broker, existing SQLite storage for canonical state, and `agent-code-cli` for inspection. `SCHEMA_VERSION=6`; no second canonical task/event store exists.

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
| `crates/agent-code-storage/{Cargo.toml,src/lib.rs,src/schema.rs,src/acc_store.rs}` | modified/new | v6 durable store |
| `crates/agent-code-storage/tests/{acc_e2e.rs,acc_migration.rs}` | new | E2E/migration |
| `crates/agent-code-cli/{Cargo.toml,src/lib.rs}` | modified | inspection API |
| `.acc-evidence/closeout-*.log`, this report | new | evidence/report |

## 8. Persistence / migration status
`SqliteAccStore` extends the existing journal with `acc_tasks`, `acc_dependencies`, `acc_context_manifests`, immutable hash/version `acc_artifacts`, and append-only sequenced/unique `acc_events`. The fixture creates an actual v4 journal with rows, migrates/reopens it, and verifies old rows plus ACC graph/context/event state. No Alembic was introduced.

## 9. ACC contract/schema status
Rust strict `serde` types are source-of-truth; unknown control fields are rejected. `schemars` generates `AccWireContractBundle`, whose deterministic hash is checked against `ACC_WIRE_SCHEMA_SHA256`. Capability, trusted capability, role, and authority remain distinct.

## 10. Runtime adapter status
`RuntimeAdapter`/`RuntimeDescriptor` are transport-neutral. `send_runtime_input` checks `midrun_input`: support invokes the adapter; lack of support returns explicit unsupported without invocation. No A2A/MCP/Codex/Claude/OpenClaw live runtime was invoked.

## 11. Exact test commands and results
| Command | Exit | Result | Log |
|---|---:|---|---|
| `cargo fmt --all -- --check` | 0 | passed | `.acc-evidence/closeout-fmt.log` |
| `cargo clippy --workspace --all-targets --all-features -- -D warnings` | 0 | passed | `.acc-evidence/closeout-clippy.log` |
| `cargo test --workspace --all-features` | 0 | 134 passed, 0 failed | `.acc-evidence/closeout-test.log` |
| `cargo test -p agent-code-team generated_wire_schema -- --nocapture` | 0 | 1 passed | `.acc-evidence/closeout-schema.log` |
| `cargo test -p agent-code-storage --test acc_migration` | 0 | 1 passed | `.acc-evidence/closeout-migration.log` |
| `cargo test -p agent-code-cli` | 0 | 1 passed | `.acc-evidence/closeout-inspection.log` |
| `git diff --check` | 0 | passed | `.acc-evidence/closeout-diff-check.log` |
The earlier 128-test result remains historical evidence; 134 is this closeout run.

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
| K | NOT_RUN | no Codex live evidence |
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
| File | SHA-256 |
|---|---|
| `.acc-evidence/closeout-fmt.log` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `.acc-evidence/closeout-clippy.log` | `ff9532e734463c311a0dc5ea935831afeca5b82a2be918c650778b3ef5f515f5` |
| `.acc-evidence/closeout-test.log` | `b737c738bd355da0646b3976e2a249a0ecc6aa058f9c7046f190b9bf14ed023e` |
| `.acc-evidence/closeout-schema.log` | `7f679d0da84d365ee9ce7d6b60fe8b3d23d5314d9b06d5fc0356b4975a01f84a` |
| `.acc-evidence/closeout-migration.log` | `6892ac995550cc530385f814d70961f5782fffca9ff84c1199eb48b39b715474` |
| `.acc-evidence/closeout-inspection.log` | `4b9693a4857dcb3e79b8f83b54b22fb32be901aee7becc380c758a330b0e704b` |
| `.acc-evidence/closeout-diff-check.log` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |

## 20. Allowed claims
- `CORE_ACC_READY`: Rust-v2 Gates A–I and N are PASSED with commands, exits, observations, and hashes above.
- Active product is Rust v2; ACC implementation language is Rust.

## 21. Forbidden claims
- Do not claim `FULL_REFERENCE_READY` or any adapter READY claim.
- Do not claim legacy Python is repaired, green, or revalidated.
- Do not treat deterministic fixtures as live adapter evidence.
