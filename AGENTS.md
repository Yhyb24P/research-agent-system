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
uv run pytest -q
uv run mypy src tests
uv run python scripts/qualification_validate.py \
  --plan examples/qualification_plan.example.json \
  --evidence examples/qualification_evidence.example.json \
  --acceptance examples/qualification_acceptance.example.json
git diff --check
```

Use `researchctl --database <path>` for the local read-only control surface.

## Structure

- `src/researchd/collaboration/`: Agent identity, selection, delegation,
  invocation, adapters, and messages.
- `src/researchd/orchestrator/`: trusted bounded workflow controller.
- `src/researchd/context/`: target-Agent context selection and egress.
- `src/researchd/storage/`: authoritative schema, migrations, and records.
- `tests/`: unit, integration, security, migration, and qualification gates.

## Current state

The Agent collaboration core plus A2A v1, Workspace Delegation, AG-UI/SSE, and
the optional LangGraph specialist pilot are implemented. Before a release
claim, require a green CI run tied to the exact commit and an immutable RC tag;
keep unverified operational qualification explicitly pending.
