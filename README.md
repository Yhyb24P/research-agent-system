# Research Agent System

[简体中文](README.zh-CN.md)

Research Agent System is an **Agent Collaboration Plane + Trusted Control
Plane** for long-running research work. Agents—not model APIs—are the
integration boundary; model, provider, protocol, and execution location are
runtime details. The controller turns agent proposals into durable,
policy-controlled, auditable execution without allowing an agent to mutate
workflow state, run arbitrary host commands, or self-certify its results.

## What this project is

The system is a Python 3.12 modular monolith built around a trusted
controller:

```text
user / CLI
   │
   ▼
trusted controller ── SQLite WAL + audit trail
   ├── bounded orchestrator
   ├── policy, budget, and human approval
   ├── local executor ── Bubblewrap sandbox
   ├── independent verifier ── content-addressed artifacts
   └── model/provider adapters
```

Models propose plans, work orders, and reviews. The controller owns state
transitions, capabilities, egress classification, approvals, verification, and
acceptance. A2A/MCP adapters and model providers are replaceable boundaries;
they are not the source of truth for research state.

The collaboration plane provides durable `AgentProfile`, `AgentRuntime`,
`Delegation`, and `AgentInvocation` records. Every plan, execution, and review
is assigned to an agent and remains traceable to its runtime snapshot. Agent
skills are descriptive; trusted capabilities are granted only by policy.

## Current deployment topology

The active setup uses `aweswitch qw` to launch a Qwen agent backed by a remote
workstation inference node:

```text
this host: controller + agent client ──> remote Qwen workstation: inference + GPU
```

This host does not load model weights and does not need a GPU. The repository
also includes an optional loopback-only `VLLMLocalModel` adapter for a vLLM
service running on the same host. A remote inference endpoint must use a
separately reviewed transport and provider policy; it is not silently treated
as local GPU execution.

## Implemented capabilities

- Durable ResearchRun/WorkOrder/Attempt/Job state with explicit transitions.
- Agent Registry, runtime health/leases, deterministic selection, Delegation,
  typed AgentInvocation, and append-only collaboration messages.
- Canonical adapters for internal, local-process, HTTP, and A2A runtimes;
  protocol tasks never replace authoritative controller records.
- Crash-aware Job submission, operation-id idempotency, cancellation, and
  restart reconciliation.
- Bubblewrap execution with no network, cleared environment, capability
  brokering, worktree isolation, and bounded resource limits.
- Content-addressed artifacts, provenance, independent verification, and
  append-only audit events.
- Deterministic policy, human approval, cloud budgets, cost accounting, and
  classified bounded retries for provider timeouts, 429s, and 5xx responses.
- Optional durable GPU admission leases with fail-closed backend behavior.
- SQLite online backup, CAS reference consistency, checksums, and restore
  health checks.
- Operational metrics for workflow records, SQLite/WAL/CAS growth, and backup
  freshness.

## Quick start

```bash
uv sync --frozen
uv run alembic upgrade head
uv run pytest -q
uv run mypy src tests
```

The repository currently exposes a library/control API rather than a standalone
daemon entrypoint. Use the integration tests as the executable reference
workflow; the local control API provides Agents, Runs, Delegations, Approvals,
Artifacts, and event/timeline queries.

Example DTOs and JSON schemas are in [`examples/`](examples/) and
[`schemas/`](schemas/). The executable qualification helpers are in
[`scripts/`](scripts/); generated manifests and runtime/evidence files are
intentionally kept outside the source baseline.

## Release status

The repository publishes immutable `v1.0.0-rc.*` candidates; use the latest
Git tag for the exact release. The V1 control plane and its
reviewed software safeguards are implemented. This repository does not claim
production Go: target-environment provider governance, off-host backup/restore,
and long-running operational soak evidence must be collected before a final
deployment decision.

GPU qualification is only applicable when local GPU-backed Jobs or same-host
vLLM execution is selected. It is not required for the current remote-Qwen
control-plane topology.

## Scope boundaries

The project does not promise universal distributed exactly-once execution, a
public A2A/MCP server, automatic cloud fallback for local failures, or hardware
GPU isolation from logical admission alone. Unsafe or unqualified boundaries
fail closed and require an explicit deployment decision.
