# Project instructions

## Positioning

`research-agent-system` is a heterogeneous Agent coding/work team.

The one job: connect Agents with different strengths to one project. High-intelligence
Agents do planning, hard reasoning, architecture, synthesis and review. Local or cheap
Agents and deterministic workers do repetitive, long-running, file-heavy, data-heavy and
tool-heavy work. Results and artifacts flow back automatically to the Agent that
continues the reasoning, with no manual copy/paste between Agents.

Communication, scheduling, recovery and safety boundaries are supporting mechanics that
let several Agents finish work. They are not the product.

The native Rust Coding Agent is the execution engine for model-backed Agents. External
Agents (Codex/Claude-style CLIs) plug in through adapters.

## Direction

The previous "Trusted Control Plane / qualification / verification" product direction is
retired. Do not extend it. The active work is the Rust v2 strangler rewrite on branch
`v2/rust-agent-team` (baseline `8cf27dc2a9e03ffbc1fbd091a576e0fb0f16bb93`).

- The Python `researchd` control-plane implementation is a frozen reference, not the
  active roadmap. Do not add features to it.
- The active roadmap is `R0 -> R8`, documented in `docs/v2/ROADMAP.md`.
- `docs/qualification/` is the frozen legacy qualification framework. Historical only.

## Do not recreate as core

Do not build these back into the product:

- `PolicyEngine` / `ApprovalService`
- a mandatory independent Verifier
- IQ/DQ/RQ qualification
- backup/DR as a product subsystem
- the `WorkOrder + Attempt + Delegation + Invocation` quartet
- trust-zone / capability / audit systems as product identity

Narrow runtime mechanics that genuinely help an Agent finish work may survive, but they
are not the product and not the roadmap.

## Engineering guards that remain

Path containment, command timeout, process-group termination, output truncation, atomic
writes, file hashes, Git checkpoints and crash recovery stay. They make a Coding Agent
reliable. They are runtime mechanics, not a control-plane product.

## Rust target

A Cargo workspace of small crates:

```text
Cargo.toml
crates/
  agent-code-core/       # session state machine, Agent loop, events, recovery
  agent-code-model/      # async model client (OpenAI-compatible HTTP first)
  agent-code-tools/      # the five atomic tools
  agent-code-workspace/  # project rules, Git worktree/checkpoint, path handling, diff/rollback
  agent-code-context/    # context budget, truncation, compaction, repository map
  agent-code-storage/    # small SQLite journal
  agent-code-team/       # Agent registry, lead, task board, scheduling, result flow
  agent-code-tui/        # ratatui/crossterm
  agent-code-cli/        # clap
```

Five atomic tools: `view_file`, `edit_file`, `write_file`, `search_dir`,
`execute_command`. `execute_command` uses structured `program + argv + cwd + timeout +
env` by default. `edit_file` uses exact unique matching, an expected file hash, and only
limited line-ending/trailing-whitespace normalization.

Required Rust CI:

```bash
cargo fmt --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

## Legacy Python (transition)

The Python package under `src/researchd/` (daemons `researchd`, `researchctl`,
`research`) is the frozen reference implementation. It still builds and tests, but it is
not the active direction. Do not extend it. R8 deletes the unreachable control-plane
modules after Rust E2E parity.

Legacy commands (still work, not the roadmap):

```bash
uv sync --frozen
uv run pytest -q
uv run mypy src tests
git diff --check
```

## Structure

- `crates/`: the Rust v2 workspace (active).
- `src/researchd/`: legacy Python control-plane reference (transition; targeted for R8).
- `docs/v2/`: the active Rust v2 roadmap and contracts.
- `docs/qualification/`: frozen legacy qualification framework, historical only.
- `tests/`: legacy Python test gates.
