# R4 — Single-Agent E2E Acceptance Record

Sealed on `v2/rust-agent-team`.

- Sealing commit: `26928698f21e50e363937877eb437f7e1ae33090`
- Base (R3 seal): `3d9ed20a6f27da10be95e219aa452ffaa509861f`
- Prepared by: qwen-agent (producer, not the acceptance reviewer)
- Reviewed by: Codex review session, final review passed with no new blocking issues
- Date: 2026-09-10

## Scope

R4 delivers the single-Agent end-to-end path: one native Rust Coding Agent
inspects, edits, tests, self-corrects, and delivers in a small real Git
repository. It is the recoverable tool-calling runtime, not a control plane.

## Implementation commits (`3d9ed20..2692869`)

```text
b6f53f3 feat(runtime): add native agent loop and tool dispatch
c4c16ee feat(model): add OpenAI-compatible HTTP client
ebbf5fe feat(runtime): add verification and delivery (N15)
ba09fa5 test(e2e): prove native agent self-correction
443e8f3 fix(R4): make tool results durable and recover the loop
7aef717 fix(R4): migrate R3 databases and meter the escaped request
2692869 fix(R4): tighten the whole N11 budget and fail on an impossible one
```

The first R4 pass (`b6f53f3..ba09fa5`) had a false-positive E2E: tool results
did not reach the model, model turns and tool requests were not durable, a lost
model request wedged the session, and the E2E hardcoded its fix values. The
three `fix(R4)` commits close these:

- Tool results reach the model. `dispatch` reshapes bounded results into
  model-readable observations carrying content (view lines plus hash, search
  hits, command output plus log references), not just counts.
- Durable turns and requests. Each model decision is journaled
  (`record_turn`); each tool request is journaled at the `Requested` boundary
  (`record_tool_requested`) with its typed payload; the verification command
  reuses the tool path with a monotonic call id.
- Lost-request recovery. A failed `decide` records the turn and leaves the
  session in `WaitingModel`; `Session::recover` resumes `WaitingModel`/
  `Verifying` to `Observing`, so the lost request is re-issued as a fresh
  turn, never replayed.
- R3 to R4 schema migration. `SqliteJournal::open` migrates an R3 database
  (adding `agent_turns.error` and `tool_calls.request`) idempotently via
  `PRAGMA user_version`.
- N11. The context builder meters the exact serialized request (after JSON
  escaping) and tightens the whole budget until it fits the input cap, failing
  with `BudgetTooSmall` when even the task framing cannot fit.

## Evidence

Local gate (at `2692869`):

- `cargo fmt --all -- --check`: clean
- `cargo clippy --workspace --all-targets -- -D warnings`: clean
- `cargo test --workspace`: 104 tests, 0 failed

Remote CI (exact commit `2692869`):

- GitHub Actions run `34476498414`, `headSha`
  `26928698f21e50e363937877eb437f7e1ae33090`, conclusion `success`.
- Steps `Format`, `Clippy`, `Test` all green.

Key tests:

- `e2e::self_correction_survives_a_lost_model_request` (in
  `crates/agent-code-runtime/tests/e2e.rs`): the full flow on a minimal code
  repo (`solve.sh` + `check.sh`). The model reads the value to fix from a
  `view_file` result and the target from the first failed check's output; a
  model request is lost mid-run, the SQLite journal is closed and reopened,
  the session is recovered, and the run continues to a clean delivery. Asserts
  every turn/request/result, monotonic call ids, both check records, the final
  diff, and that the original worktree is untouched.
- `r3_database_migrates_preserving_data` (in
  `crates/agent-code-storage/src/lib.rs`): an R3 database is migrated; old
  data is preserved, the new columns are writable, and the session recovers.
- `bounded_context_absorbs_json_escaping_growth`,
  `bounded_context_trims_task_rules_map_escaping`, and
  `bounded_context_fails_when_even_task_cannot_fit` (in
  `crates/agent-code-runtime/src/agent.rs`): N11.

## Acceptance criteria (N-matrix)

- N15 delivery includes diff, checks and known failures: delivered by R4.
- N13 restart recovers workspace/session: strengthened by R4 (a lost model
  request recovers and continues).
- N14 interrupted non-idempotent command is not blindly replayed: extended
  to a lost model request, re-issued as a fresh turn.
- N11 model context stays within configured budget: the JSON-escaping gap is
  closed; the full serialized request is metered.
- N01 and N03-N12 carried from R2/R3 and exercised by the E2E.

## Sealing (fact-based principle)

A milestone is sealed only after the implementation-side commit plus the
exact-commit remote CI are both green. Both hold for R4:

- implementation side: `2692869`, local gate green (104 tests).
- exact-commit CI: run `34476498414` for `2692869`, conclusion `success`.

## Known limitations / deferred

- `b6f53f3` carries a `Co-Authored-By: Claude Opus 4.8` trailer from a prior
  session (misattributed; this work is Qwen Code). Left as-is by decision;
  cosmetic only, not a functional issue.
- R5 (Team) has a local implementation under review but is not sealed. See
  [`R5_STATUS.md`](R5_STATUS.md).
