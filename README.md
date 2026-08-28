# Research Agent System

[中文版 / Chinese](README.zh-CN.md)

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
RCs and qualification evidence are maintained outside the source repository;
the deployment decision remains target-environment dependent.
The control plane and agent process do not need GPU resources to run. The
current deployment uses `aweswitch qw` to launch the Qwen agent against the
remote workstation inference node; GPU and model weights belong to that remote
node, not this host. The repository also contains an optional loopback-only
`VLLMLocalModel` path for a same-host vLLM service. Remote inference transport
and provider policy must be qualified separately and are not silently treated
as local GPU execution.

Run the full regression suite with:

```text
.venv/bin/pytest
.venv/bin/mypy
```

Qualification reports and operational evidence are intentionally not committed
to this GitHub repository.
