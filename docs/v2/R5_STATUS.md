# R5 — Team Layer Status (Unsealed)

Snapshot date: 2026-09-11.

R5 is **implemented locally but not accepted or sealed**. The current local
head is `677720f5c0f03e6a6d00d6160703ac1557e2098e` on
`v2/rust-agent-team`; the remote branch remains at
`691f2e6` (the docs-only R4 acceptance-record commit). No R5 commit has been
pushed or run through exact-commit remote CI.

## What is present locally

The local R5 stack is `096bc79..677720f`:

```text
096bc79  registry and deterministic tier routing
556bc3b  durable SQLite task board and v2 -> v3 migration
5076245  concurrent scheduler, retry, and reassignment
f9c6678  Lead plan / follow-up / synthesis loop
e43a336  durable-team end-to-end test
0a7acaa  parent result in follow-up context
677720f  first R5 review fixes
```

It adds the `agent-code-team` registry, task board, scheduler and Lead loop,
plus a SQLite implementation in `agent-code-storage`. Local tests include
deterministic Reasoner, Worker and Utility mocks; they do **not** connect Qwen,
a high-intelligence hosted Agent, or an external Coding-Agent CLI. Those are
R6 work (T14 and T15).

At `677720f`, local gates passed:

```text
cargo fmt --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

The reported local test count is 123 passed. This is local evidence only, not
an R5 acceptance result.

## Current review blockers

The following review findings remain unresolved at this snapshot. They are why
R5 must not be described as sealed and why no R5 acceptance record exists.

1. **Queued work is marked Running too early.** The scheduler records a
   `Running` attempt before acquiring the per-Agent semaphore. A task waiting
   for capacity can therefore look as though its driver may already have run.
   Acquire the permit first, then persist `Running` immediately before calling
   the driver; a queued task must remain `Pending` or `Assigned`.
2. **There is no production interrupted-task recovery path.** The current
   test manually appends a later successful attempt to a task with a prior
   `Running` attempt. Implement and test a public recovery operation that
   closes an interrupted attempt (without blindly replaying possible side
   effects), records the next attempt, and routes it according to policy.
3. **Final `TeamResult.task_refs` are not stored exactly.** The final answer
   is durable on the root task, but reconstruction currently derives refs from
   all successful descendants. Persist the Lead-selected refs and prove that a
   final result selecting a subset of successful tasks round-trips exactly.
4. **Some task-board errors and result-flow writes are still not durable at
   the right boundary.** Parent-result lookup in the scheduler still suppresses
   a board read error. Propagate it. Also persist messages and artifacts with
   the successful attempt before returning from the worker path, rather than
   later while collecting task handles; otherwise a crash can retain a success
   summary while losing its result flow.

These findings do not change R4's sealed status. They define the required next
`fix(R5)` before another acceptance review.

## Acceptance state

- R4: sealed at `2692869`, with exact remote CI run `34476498414` successful.
- R5 T01-T13 and T16: locally exercised in part, **pending review closure and
  exact-commit CI**.
- R5 T14 (local Qwen) and T15 (high-intelligence real Agent): deferred to R6.
- R6-R8: not started.

## Scope guardrails

R5 must remain a small team layer. Do not introduce PolicyEngine,
ApprovalService, a mandatory independent Verifier, qualification workflows, a
generic workflow DSL, or the legacy control-plane model. The legacy Python
implementation and `docs/qualification/` remain frozen historical reference.
