# Research Agent System

This repository implements the trusted local control plane described by the
Cloud Research Lead + Local Research Executor handoff pack.

The implementation has completed TASK00–TASK08 and the reviewed qualification
hardening checkpoints. It is a modular-monolith V1 control plane with SQLite WAL persistence, immutable
content-addressed artifacts, deterministic policy and verification, isolated
local execution, outbound-only structured Cloud Lead calls, bounded
orchestration, optional A2A/MCP boundary adapters, operational metrics, and
backup/restore utilities.

The trusted controller remains authoritative: models propose, local workers
execute, verifiers prove, policy controls, and humans authorize. The project
does not expose a public A2A/MCP endpoint, silently fall back from local to
cloud models, or claim exactly-once scheduler semantics.

Current status: **V1 control plane complete; deployment qualification pending**.
RCs, DQ00–DQ06 evidence requirements, and the current Go/No-Go decision are in
`docs/DQ00_RELEASE_BASELINE.md` through `docs/DQ06_PRODUCTION_GO_NO_GO.md`.
The control plane and agent process do not need GPU resources to run. A local
model may instead be loaded by a vLLM inference node and called from the agent
over the loopback-only `VLLMLocalModel`; in that topology GPU belongs to the
inference node, while GPU admission is an explicit contract for GPU-backed
job execution. Remote inference endpoints require a separately reviewed
transport adapter and are not silently enabled.

Run the full regression suite with:

```text
.venv/bin/pytest
.venv/bin/mypy
```

Task-specific reports and limitations are in `docs/TASK00_CHANGELOG.md`
through `docs/TASK08_CHANGELOG.md`; operations and the bounded pilot are
documented in `docs/OPERATIONS_RUNBOOK.md` and `docs/PILOT_REPORT.md`.
