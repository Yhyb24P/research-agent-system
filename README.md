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
Human / researchctl
        │
        ▼
Agent Collaboration Plane
Registry / Runtime / Delegation / Invocation / Context
        │
        ▼
Trusted Control Plane
Run / WorkOrder / Attempt / Policy / Approval / Verification / Audit
        │
        ▼
Agent Runtime Adapters
internal / process / HTTP / A2A
```

Agents propose plans, carry out work orders, and return reviews. The controller owns state
transitions, capabilities, egress classification, approvals, verification, and
acceptance. A2A/MCP and runtime adapters are replaceable boundaries;
they are not the source of truth for research state.

The collaboration plane provides durable `AgentProfile`, `AgentRuntime`,
`Delegation`, and `AgentInvocation` records. Every plan, execution, and review
is assigned to an agent and remains traceable to its runtime snapshot. Agent
skills are descriptive; trusted capabilities are granted only by policy.

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
- Deterministic policy, human approval, bounded runtime calls, and classified
  retries for transient adapter failures.
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
uv run researchctl --database researchd.db agent list
```

The repository exposes a library, a loopback control API, and the read-only
`researchctl` command. Integration tests are the executable reference workflow.
The control surface provides Agents, Runs, Delegations, Approvals, Artifacts,
and event/timeline queries. State-changing commands require an explicitly wired
controller instance.

Example DTOs and JSON schemas are in [`examples/`](examples/) and
[`schemas/`](schemas/). The executable qualification helpers are in
[`scripts/`](scripts/); generated manifests and runtime/evidence files are
intentionally kept outside the source baseline.

## Release status

The repository publishes immutable `v1.0.0-rc.*` candidates; use the latest
Git tag for the exact release. The control plane and its
reviewed software safeguards are implemented. This repository does not claim
production Go: target-environment runtime/transport governance, off-host backup/restore,
and long-running operational soak evidence must be collected before a final
deployment decision.

## Scope boundaries

The project does not promise universal distributed exactly-once execution, a
public A2A/MCP server, or implicit fallback between Agents. Unsafe or
unqualified boundaries fail closed and require an explicit deployment decision.
