# Project instructions

## Positioning

`research-agent-system` is an Agent Collaboration Plane plus Trusted Control
Plane. Agent is the integration identity; runtime implementation details and
protocols are subordinate to `AgentRuntime`.

## Required invariants

- Planning, execution, and review enter `ResearchOrchestrator` through
  `CollaborationGateway`, `Delegation`, and `AgentInvocation`.
- Agent skills are descriptive and never grant trusted `Capability` values.
- Verification, policy, orchestration, and job management remain trusted
  system actors and cannot be registered as ordinary Agents.
- A2A and MCP remain adapters; protocol tasks are not authoritative workflow
  records.
- Workspace grants and AG-UI events remain bounded transport/projection
  records; LangGraph may implement an Agent runtime but never orchestration.
- Preserve append-only audit history, immutable artifacts, explicit state
  transitions, and fail-closed security behavior.
- Keep repository usage documentation in `README.md` and `README.zh-CN.md`.
  Tracked documentation under `docs/` is limited to `docs/qualification/`;
  other documentation trees require an explicit governance change.

## Commands

```bash
uv sync --frozen --extra a2a --extra langgraph-agent --extra qualification
uv run alembic upgrade head
uv run researchd --config researchd.json init
uv run researchd --config researchd.json serve
uv run pytest -q
uv run mypy src tests
uv run python scripts/qualification_validate.py \
  --plan examples/qualification_plan.example.json \
  --evidence examples/qualification_evidence.example.json \
  --acceptance examples/qualification_acceptance.example.json
git diff --check
```

Use `researchd init` only for a fresh database and `researchd serve` for the
readiness-gated loopback daemon. Normal startup never upgrades a schema. Use
`researchctl --database <path>` for the local read-only inspection surface.
The JSON daemon configuration rejects unknown fields and requires absolute
paths, loopback binding, repository IDs, and fixed argv arrays for job types.
Use `researchd --config <path> validate` or `inspect` before initialization;
both are read-only and inspect never echoes fixed command arguments.

## Structure

- `src/researchd/collaboration/`: Agent identity, selection, delegation,
  invocation, adapters, and messages.
- `src/researchd/orchestrator/`: trusted bounded workflow controller.
- `src/researchd/context/`: target-Agent context selection and egress.
- `src/researchd/storage/`: authoritative schema, migrations, and records.
- `tests/`: unit, integration, security, migration, and qualification gates.

## Current state

PX00–PX09 are implemented and closed by their recorded exact-head CI evidence.
The daily `research` client, managed planner/coder/reviewer flow, collaboration
and handoff projections, detached consoles, governed A2A attachment, and the
bounded external CLI bridge are present. PX09 Browser Control Tower, including
the loopback browser launcher, collaboration/Agent-console projections and
event-offset stream, closed at `40d83ec` after Qwen's independent matrix and
exact-head CI `33351341328`. Keep future work on a new explicitly scoped plan;
do not reopen a closed productization item with compatibility patches.

Before any release claim, require a green CI run tied to the exact commit and an
immutable RC tag; keep unverified operational qualification explicitly pending.
