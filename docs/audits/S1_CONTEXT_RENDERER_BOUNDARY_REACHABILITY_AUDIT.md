# S1 Context Renderer Boundary & Reachability Audit

The single question this audit answers:

> Can the renderer's L2 whole-payload truncation happen in a state the current
> product can really reach?

**Answer: yes — reproduced in a real product run.** No fix is applied in this
round, per the task contract.

```text
S1_RENDERER_BOUNDARY_AUDIT_COMPLETE=true
S1_PRODUCT_REACHABILITY_COMPLETE=true
S1_REAL_SCENARIO_EVIDENCE_COMPLETE=true

L2_ACTIVATION_BOUNDARY_CHARACTERIZED=true
MALFORMED_JSON_MECHANICALLY_POSSIBLE=true
MALFORMED_JSON_PRODUCT_REACHABLE=true
REAL_RUN_REPRODUCED=true
CONTEXT_RENDERER_CORRECTNESS_RISK=true

RENDERER_IMPLEMENTATION_CHANGED=false
STATUS_SUMMARY_TRACE_DESIGNED=false
JCODEMUNCH_INTEGRATED=false
PROCESS_SUPERVISOR_LINE_CLOSED=true
```

## 1. Identity and baseline

```text
base_sha           8088252ab06c23d6a1a0083dd7013e0a2867d21b (canonical main)
branch             audits/context-renderer-boundary-reachability
machine            same host/toolchain as S0 (rustc/cargo 1.94.1)
default_max_prompt_bytes  32768      (team_runner.rs:52)
default_max_tasks         32         (team_runner.rs:48)
default_max_rounds        8          (team_runner.rs:46)
default_max_retries       2          (team_runner.rs:50)
schema_version            12
renderer_sha        codex_lead.rs unchanged; verified byte-identical to main
```

Precondition note, recorded rather than assumed: the corrected S0 audit
(`docs/audits/S0_CONTEXT_FLOW_BYTE_BUDGET_AUDIT.md`, PR #20) was still open and
not in canonical `main` when this audit ran. The S1 baseline is therefore the
source on `main`; the renderer files are byte-identical between `main` and the
S0 branch, so no S1 conclusion depends on the unmerged document.

## 2. Actual renderer model (re-read from source, not from the S0 report)

```text
render_prompt  : budget = max_prompt_bytes - PROMPT_PREFIX.len() - PROMPT_SUFFIX.len() - 2
                 PROMPT_PREFIX = 37 bytes, PROMPT_SUFFIX = 175 bytes
                 budget = 32768 - 37 - 175 - 2 = 32554        codex_lead.rs:240-247

render_context : entries  = results + artifacts + failures + messages + 1
                 L1       = per_text = (budget / entries.max(1)).clamp(64, 4096)
                 L1 fields: objective, result summary, artifact path, failure error,
                            message body
                 unbounded by L1: candidate ids, artifact sha256, message from/to,
                            task ids, round, root_task_id, JSON envelope
                 L2       = bound_utf8(&payload.to_string(), budget)   codex_lead.rs:257-313

bound_utf8     : UTF-8-safe prefix (backs up to a character boundary), not
                 JSON-structure-aware                              codex_lead.rs:613-622
```

## 3. S1-A — L2 activation boundary with the actual Rust renderer

Method: an isolated worktree (`/tmp/ctx-s1/probe`, detached at the base) with a
temporary `#[cfg(test)]` harness inside `codex_lead.rs` that calls the real
`CodexLeadBrain::render_context(&ctx, budget)` and mirrors only the pre-L2
serialization to measure the size L2 sees. Probe code was never committed; the
worktree is discarded. Probe validation came first, as required:

```text
CTRL-over-budget   serialized_before_L2=660178 > budget=32554
                   L2_truncated=true, json_valid=false
=> the probe can detect both L2 activation and malformed output
```

Single-variable searches (exponential + binary) for the first case where L2
fires. Budget 32,554 in every row.

| case | first activation at | entities | serialized_before_L2 | rendered | L1 reduced | L2 | JSON valid |
|---|---|---:|---:|---:|---|---|---|
| baseline (1 candidate, small objective) | – | 1 entry | 185 | 185 | no | no | yes |
| objective 4,096 / 100,000 bytes | never | 1 entry | 4,217 | 4,217 | no | no | yes |
| candidates, 16-char ids | **1,705 candidates** | 1 entry | 32,573 | 32,554 | no | **yes** | **no** |
| results, 16-byte summaries | **722 results** | 723 entries | 32,568 | 32,554 | yes | **yes** | **no** |
| results, 4,096-byte summaries | **31 results** | 32 entries | 32,571 | 32,554 | yes | **yes** | **no** |
| artifacts (24-char path, 64-hex digest) | **285 artifacts** | 286 entries | 32,564 | 32,554 | yes | **yes** | **no** |
| failures (16-byte error) | **756 failures** | 757 entries | 32,586 | 32,554 | yes | **yes** | **no** |
| messages (64-byte body, 8-char ids) | 295 messages | 296 entries | 32,634 | 32,554 | yes | **yes** | **no** |
| messages (64-byte body, 1,024-char ids) | 16 messages | 17 entries | 34,456 | 32,554 | yes | **yes** | **no** |
| mixed: 900 candidates + 180 artifacts | – | 181 entries | 37,687 | 32,554 | yes | **yes** | **no** |
| mixed: 900 candidates + 32 results + 32 failures | – | 65 entries | 51,028 | 32,554 | yes | **yes** | **no** |
| **real configuration** (one 33,000-char agent id) | – | 1 entry | **33,220** | 32,554 | no | **yes** | **no** |

Findings:

```text
first L1 reduction             8 entries (per_text 4,069, below the 4,096 ceiling)
first L2 activation            candidate count, ~1,705 ids of 16 chars
first malformed rendered JSON  the same case as the first L2 activation
first candidate-dominated      candidates alone reach L2 (no L1 involvement)
first envelope/metadata-only   not reachable without an unbounded field; the
                               envelope alone is ~185 bytes
```

Everything in this section is `MECHANICALLY_POSSIBLE`. Whether the product can
reach such a state is section 4, and it is a separate proof.

## 4. S1-B — product reachability

| Dimension | Production writer / public input | Default bound | Hard bound | Classification | Evidence |
|---|---|---|---|---|---|
| candidates | `agent_registry` rows; `am agent add <id> …` (public) | none | **none: no id length and no agent-count validation** | **REACHABLE** | `registry_store.rs:51-67` inserts the id verbatim; `agent.rs`/`registry.rs` contain no id length/charset/count check; a 33,000-character id was accepted and stored (`id_len=33000`) |
| artifacts | driver `artifact_paths` from repeatable `--artifact RELPATH`; rows written per task on success | none per task | **none: no count cap on `artifact_paths`** | **REACHABLE** (config), not re-reproduced | `driver_factory.rs:417` `string_array(...)` has no length cap; every driver collects configured paths (`collect_artifacts`) |
| results | succeeded descendants; `Lead::context` reads every child's attempt result | `DEFAULT_MAX_TASKS=32` under `am run` | `run-team/resume-team --max-tasks N` parses any `u32 > 0` (`parse_positive_u32`, advanced.rs:785-793) | **REACHABLE only through the hidden compatibility surface**; UNREACHABLE via `am run` | 722 results needed; `am run` cannot exceed 32 children |
| failures | failed descendant attempts, bounded error text | same as results | same as results | **REACHABLE only through the hidden compatibility surface** | 756 failures needed |
| messages | Lead-addressed `messages` rows | none | **no production writer** | **UNREACHABLE_UNDER_CURRENT_PRODUCT / NO_PRODUCT_WRITER** | every product driver returns `message: None` (`acp_worker.rs:677,700,823`, `claude_cli_driver.rs:212`, `codex_team_driver.rs:210`, `codex_exec_driver.rs:309`); `record_message` production call sites: none (only the trait, the impl, tests) |
| objective | `am run "<objective>"` free text | L1-bounded at render time | user input length unbounded, but L1 caps it at `per_text` | **UNREACHABLE** for L2 | measured: a 100,000-byte objective renders as 4,217 bytes and never reaches L2 |
| runtime events | observation plane | n/a | n/a | **UNREACHABLE** (not rendered) | no event field in `LeadContext` |

So the two vectors that reach L2 **through the public CLI alone** are candidate
ids and configured artifact paths; results/failures need the hidden
compatibility command; messages have no writer at all; the objective cannot.

## 5. S1-C — minimal real reproduction

The cheapest reachable vector is a candidate id, because agent ids are
unvalidated. Minimal reproduction, using only public commands:

```bash
am init .                      # project
am agent add lead --role reasoner --adapter codex-exec -- codex
am agent add "$(python3 -c 'print("w"*33000)')" --role worker --adapter acp -- qwen --acp
am run --quiet "Delegate one bulk task to the worker: write done.txt containing ok."
```

Observed:

```text
am agent add accepted the 33,000-character id (add rc=0, registry id_len=33000)
am run spawned a real codex-exec Lead; the run failed with
  "the lead run failed: lead brain unavailable: runtime execution timed out"

The Lead's own Codex session transcript (from the real spawned process) contains
the delivered prompt. Extracted context segment:
  context_segment_bytes = 32554          (exactly the L2 budget)
  ends_with_closing_brace = false
  tail_60 = 'wwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwwww'
  context_is_valid_json = false  ("Unterminated string starting at: line 1 column 161")
  the full 33,000-character id is absent from the prompt (truncated away)
=> REAL_RUN_REPRODUCED
```

This is a real product run, a real external Lead runtime and a real delivered
prompt, not a synthetic harness. The context the Lead received is a strict
prefix of a JSON document and cannot be parsed as JSON.

Scope of the claim: the run failed with a timeout, not with a JSON-decode error,
because the Lead runtime never parses the context itself — the *product* hands
the malformed context to the model. This audit therefore establishes the
renderer correctness risk, not a specific failure mode of any one provider.

## 6. Confirmed risk and separate mechanical facts

```text
PRODUCT_REACHABLE + REAL_RUN_REPRODUCED:
  a public-CLI configuration (an unvalidated agent id) makes the rendered Lead
  context exactly L2-truncated and therefore not valid JSON
  -> CONTEXT_RENDERER_CORRECTNESS_RISK=true

MECHANICALLY_POSSIBLE but not product-reachable today:
  messages vectors (295 messages with 8-char ids; 16 messages with 1,024-char
  ids) — no production writer exists, so no real run can reach them

MECHANICALLY IMPOSSIBLE:
  objective length alone (L1 bounds it), envelope-only growth
```

## 7. Open questions (not answered here)

1. Whether a truncated context ever changes a model's decision in practice (this
   run's Lead timed out before producing one) — needs a decision-level study.
2. Whether artifact-path configurations reach L2 in a real run (mechanically
   established and config-reachable; not reproduced).
3. Whether the hidden `--max-tasks` surface should expose an upper bound.
4. Whether agent ids need a product-level length/format bound at all.

## 8. Decision for the next research step

```text
CONTEXT_OPTIMIZATION_JUSTIFIED=BLOCKED_BY_RENDERER_CORRECTNESS
```

Per the task contract this round stops here: the renderer is **not** fixed, no
`Status`/`Summary`/`Trace` design is started, and jCodeMunch stays out of scope.
The next step is a minimal renderer-correctness experiment (for example: make L2
structurally aware, or bound the unbounded fields), to be authorized separately.

## 9. Explicit non-goals

No renderer change, no context abstraction, no prompt optimization, no schema or
wire change, no `Status`/`Summary`/`Trace`, no jCodeMunch, no ProcessSupervisor
work. The probe harness lived only in an isolated worktree and was discarded.

## 10. Evidence (ephemeral)

```text
probe worktree            /tmp/ctx-s1/probe (detached at base; harness removed)
probe raw output          /tmp/ctx-s1/s1a-raw.txt, /tmp/ctx-s1/s1a-real-row.txt
reproduction project      /tmp/ctx-s1/repro (SQLite state + log)
reproduction run log      /tmp/ctx-s1/repro-run.log
lead session transcript   the Codex rollout of the spawned Lead process
                          (~/.codex/sessions/2026/09/16/rollout-2026-09-16T08-15-08-*.jsonl)
```

These are local scratch artifacts; the repository keeps the summarized
measurements above and the machine-readable matrix beside this file.

## Final status

```text
S1_RENDERER_BOUNDARY_AUDIT_COMPLETE=true
S1_PRODUCT_REACHABILITY_COMPLETE=true
S1_REAL_SCENARIO_EVIDENCE_COMPLETE=true

RENDERER_IMPLEMENTATION_CHANGED=false
SCHEMA_CHANGED=false
WIRE_CHANGED=false

L1_BOUND_CHARACTERIZED=true
L2_ACTIVATION_BOUNDARY_CHARACTERIZED=true

L2_TRUNCATION_IMPLIES_INVALID_JSON=true
MALFORMED_JSON_MECHANICALLY_POSSIBLE=true
MALFORMED_JSON_PRODUCT_REACHABLE=true

MESSAGE_PRODUCT_WRITER=false
FAILURE_CONTEXT_REACHABLE=REACHABLE_ONLY_VIA_HIDDEN_MAX_TASKS

CONTEXT_RENDERER_CORRECTNESS_RISK=true
CONTEXT_OPTIMIZATION_JUSTIFIED=BLOCKED_BY_RENDERER_CORRECTNESS

STATUS_SUMMARY_TRACE_DESIGNED=false
JCODEMUNCH_INTEGRATED=false
PROCESS_SUPERVISOR_LINE_CLOSED=true
```
