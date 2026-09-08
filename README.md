# Research Agent System

[简体中文](README.zh-CN.md)

> **Status.** The active direction is a Rust v2 rewrite of the native Coding Agent and
> the heterogeneous Agent team layer, on branch `v2/rust-agent-team`. The Python
> `researchd` control-plane implementation is a frozen reference and is no longer the
> product. See [docs/v2/ROADMAP.md](docs/v2/ROADMAP.md).

Research Agent System is a **heterogeneous Agent coding/work team**.

The one job: connect Agents with different strengths to one project. High-intelligence
Agents do planning, hard reasoning, architecture, synthesis and review. Local or cheap
Agents and deterministic workers do repetitive, long-running, file-heavy, data-heavy and
tool-heavy work. Results and artifacts flow back automatically to the Agent that
continues the reasoning, with no manual copy/paste between Agents.

Communication, scheduling, recovery and safety boundaries are supporting mechanics that
let several Agents finish work. They are not the product.

## Architecture

```text
User
  |
  v
Team Session
  |
  v
Lead / Reasoning Agent
  | delegate
  +------------------+-------------------+
  v                  v                   v
Reasoning Agent    Local Model Agent   Utility Worker
  |                  |                   |
  +----- result / files / messages ------+
                       |
                       v
              Lead integrates result
                       |
                       v
                    Deliver
```

Each native model-backed Agent runs the same internal loop:

```text
Init -> Observe -> Model Decision -> Tool Execution -> Observe -> ...
     -> Verify -> Deliver / Rollback
```

## Native Coding Agent

The native Rust Coding Agent is a recoverable tool-calling runtime. It exposes five
atomic tools:

- `view_file` — workspace-contained, paginated, returns a file hash.
- `edit_file` — exact unique match, expected file hash, limited line-ending/trailing
  whitespace normalization, atomic write, syntax guard with rollback.
- `write_file` — new files or explicit short-file replacement, with size bounds.
- `search_dir` — bounded path/line/match records, never whole files.
- `execute_command` — structured `program + argv + cwd + timeout + env` by default, with
  process-group termination and output truncation.

Reliability mechanics (path containment, command timeout, worktree isolation, output
truncation, atomic writes, rollback) are kept because they make a Coding Agent reliable.
They are runtime mechanics, not a control-plane product.

## Team layer

The team layer only divides work and moves results between Agents. It does not become an
enterprise workflow engine. Agents have a tier (`Reasoner`, `Worker`, `Utility`), a
driver, and a concurrency bound. Routing is deterministic: reasoning/review goes to a
Reasoner, bulk/tool work goes to a Worker or Utility, an explicit target wins, otherwise
the configured default. A worker result automatically becomes context for its parent task,
the Lead, and any explicitly addressed Agent.

Drivers:

- `NativeCodingAgentDriver` — the Rust state machine + model client + five tools.
- `ExternalCliAgentDriver` — runs an external Coding Agent CLI; it is not wrapped in a
  second tool loop.
- `UtilityDriver` — deterministic worker for tests/build/search/batch.

## Roadmap

The active plan is `R0 -> R8` in [docs/v2/ROADMAP.md](docs/v2/ROADMAP.md):

- R0 direction reset (this repositioning)
- R1 Rust core (workspace, state machine, SQLite journal, model trait, recovery)
- R2 tools and workspace
- R3 context budget and recovery
- R4 single-Agent E2E
- R5 team scheduler
- R6 real Agents (high-intelligence + local Qwen)
- R7 TUI cutover
- R8 delete legacy

The two blocking E2Es are: a single native Agent inspecting, editing, testing,
self-correcting and delivering a patch in a small real Git repository; and a team where a
Lead delegates at least two tasks, local/utility workers perform the work, results and
artifacts flow back, and the Lead uses them to produce the final answer.

## Rust development

```bash
cargo fmt --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

## Legacy Python (frozen reference)

The Python package under `src/researchd/` (daemons `researchd`, `researchctl`,
`research`) is the frozen reference implementation. It still builds and tests, but it is
not the active direction. Do not extend it. R8 deletes the unreachable control-plane
modules after Rust E2E parity.

Essential legacy commands:

```bash
uv sync --frozen
uv run pytest -q
uv run mypy src tests
git diff --check
```

Full legacy operational detail (control API routes, daemon commands, Browser Control
Tower, TUI, backup/restore) is preserved in the frozen baseline
`8cf27dc2a9e03ffbc1fbd091a576e0fb0f16bb93` and in git history. The legacy qualification
framework lives in `docs/qualification/` and is historical only.

## License

Apache License 2.0 (ALv2). See the `LICENSE` file at the repository root.
