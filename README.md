# Research Agent System

[![control-plane-quality](https://github.com/Yhyb24P/research-agent-system/actions/workflows/quality.yml/badge.svg)](https://github.com/Yhyb24P/research-agent-system/actions/workflows/quality.yml)

[简体中文](README.zh-CN.md)

Research Agent System is an **Agent Collaboration Plane + Trusted Control
Plane** for durable, policy-controlled research workflows. The integration
identity is an Agent. Frameworks, providers, protocols, and execution
locations are implementation details of an `AgentRuntime`.

Agents may propose, execute, review, and perform specialist analysis. They do
not own workflow state, grant themselves capabilities, approve their own
actions, or verify their own results. Those authorities remain in the trusted
control plane.

## Architecture

```text
Human / Browser / researchctl
          │ typed commands + read-only AG-UI projection
          ▼
Local Control API ───────────────► Trusted Control Plane
                                      │
                         ResearchRun / WorkOrder / Attempt
                         Policy / Approval / Audit / Verifier
                                      │
                                      ▼
                            CollaborationGateway
                                      │
                     Delegation / AgentInvocation / Context
                         ┌────────────┼────────────┐
                         ▼            ▼            ▼
                    internal/HTTP   A2A v1     LangGraph
                         │            │        specialist Agent
                         └────────────┴────────────┘
                                      │
                         Workspace Grant / Lease
                         Git or Archive transport
                                      │
                                      ▼
                         Artifact reconciliation
                                      │
                                      ▼
                              Independent Verifier
```

SQLite records are authoritative. A2A tasks, AG-UI events, workspace transport
handles, and LangGraph state are adapter/runtime representations; none of them
replace `ResearchRun`, `Delegation`, `AgentInvocation`, `Artifact`, or
`AuditEvent` records.

## Implemented capabilities

- Agent registry, runtime leases, deterministic role/skill/trust-zone
  selection, immutable assignment snapshots, Delegations, and typed
  AgentInvocations.
- A2A v1 Agent Cards, Task/Message/Artifact codecs, official Python SDK client,
  tenant propagation, task listing, cancellation, streaming aggregation, and
  `ExecutorResult` decoding through `CollaborationGateway`.
- Bounded Workspace Delegation with separate grants, path/classification/size
  admission, leases, Git-worktree and Archive transports, artifact-only
  reconciliation, and cleanup state.
- Durable runs, work orders, attempts, jobs, explicit state transitions,
  operation idempotency, cancellation, and restart reconciliation.
- Deterministic policy, scoped human approval, classified Agent context, and
  fail-closed capability brokering.
- Content-addressed artifacts, provenance, observations, claims, independent
  verification, review, and append-only audit history.
- Database-assigned monotonic event offsets, read-only AG-UI projection, SSE
  replay/follow with `Last-Event-ID`, and typed cancel/approve/human-decision
  commands.
- Optional LangGraph Agent runtime. The included `agent_research_critic` pilot
  runs a real compiled graph and returns a structured specialist result while
  researchd remains authoritative.
- Loopback JSON control API, static TUI renderer, SQLite backup/restore checks,
  operational metrics, and lock-derived SBOM generation.

## Requirements and installation

- Linux and Python `>=3.12,<3.13`.
- [`uv`](https://docs.astral.sh/uv/) for locked dependency installation.
- Bubblewrap for the sandbox/security test suite.

Install the core only:

```bash
uv sync --frozen
```

Install all currently supported Agent runtime extras:

```bash
uv sync --frozen --extra a2a --extra langgraph-agent
```

The extras remain outside the trusted domain/storage/policy core.

## Initialize and test

Create or upgrade the local controller database:

```bash
uv run alembic upgrade head
```

Run the complete software gate:

```bash
uv run pytest -q
uv run mypy src tests
git diff --check
```

The post-RC qualification mainline, Gate policy, and executable evidence
contracts are documented in
[`docs/qualification/`](docs/qualification/README.md). Validate their example
bundle with:

```bash
uv sync --frozen --extra qualification
uv run python scripts/qualification_validate.py \
  --plan examples/qualification_plan.example.json \
  --evidence examples/qualification_evidence.example.json \
  --acceptance examples/qualification_acceptance.example.json
```

Run the four interoperability/workspace pilots directly:

```bash
uv run pytest -q \
  tests/integration/test_protocol_adapters.py \
  tests/integration/test_workspace.py \
  tests/integration/test_agui.py \
  tests/integration/test_langgraph_runtime.py
```

Integration tests are the executable reference workflow. The repository is a
library/modular-monolith baseline; it does not yet ship a production daemon or
browser application bootstrap.

An embedding controller must register its workspace transports and call
`WorkspaceDelegationService.recover_incomplete()` during startup, before it
accepts new work. This closes interrupted provisioning/reconciliation windows
using the transport handle persisted before the external side effect.
It must likewise construct `WorktreeManager` with durable sessions and call
`recover_incomplete(repository_mapping)` before creating attempt worktrees.
This closes interrupted create/remove windows from their persisted lifecycle
state.

Qualify the actual deployment filesystem rather than a temporary substitute:

```bash
uv run python scripts/dq01_preflight.py --strict --target <deployment-root>
uv run python scripts/dq01_filesystem_probe.py --root <deployment-root>
```

## Inspect the control plane

`researchctl` opens an existing database without constructing an orchestrator:

```bash
uv run researchctl --database researchd.db run list
uv run researchctl --database researchd.db agent list
uv run researchctl --database researchd.db events <run-id> --after <stream-offset>
```

Start the loopback read API for an existing database:

```bash
uv run python - <<'PY'
from pathlib import Path
from researchd.api.control import LocalControlAPI
from researchd.api.web import serve_local_control
from researchd.storage.db import create_sqlite_engine, session_factory

sessions = session_factory(create_sqlite_engine(Path("researchd.db")))
serve_local_control(LocalControlAPI(sessions)).serve_forever()
PY
```

Then query it from another terminal:

```bash
curl http://127.0.0.1:8788/api/runs
curl http://127.0.0.1:8788/api/events/<run-id>?after=0
curl -N http://127.0.0.1:8788/api/runs/<run-id>/stream?follow=1
```

The read-only bootstrap intentionally rejects state-changing commands. An
embedding application must construct `LocalControlAPI` with a
`ResearchOrchestrator`; typed commands then enter the existing policy/state
machine through these routes:

| Method | Route | Body |
|---|---|---|
| `POST` | `/api/runs/{run_id}/cancel` | `{}` |
| `POST` | `/api/work-orders/{work_order_id}/approve` | `{"grant_id":"..."}` |
| `POST` | `/api/work-orders/{work_order_id}/human-decision` | `{"action":"abort"}` or `{"action":"revise","objective":"..."}` |

Arbitrary UI events have no mutation endpoint.

## Repository map

- `src/researchd/collaboration/`: Agent contracts, registry, selection,
  delegation, invocation, adapters, messages, and LangGraph runtime.
- `src/researchd/workspace/`: workspace grants, admission, transport, lease,
  reconciliation, and cleanup.
- `src/researchd/orchestrator/`: trusted bounded workflow controller.
- `src/researchd/api/`: local control facade, AG-UI projection, SSE/JSON HTTP,
  and TUI rendering.
- `src/researchd/storage/`: authoritative SQLAlchemy records and Alembic
  migrations.
- `src/researchd/policy/`, `verifier/`, `artifacts/`, `executor/`: trusted
  enforcement and evidence path.
- [`examples/`](examples/) and [`schemas/`](schemas/): versioned DTO examples
  and JSON schemas.
- [`scripts/`](scripts/): qualification, release-manifest, and SBOM helpers.
- `tests/`: contract, integration, migration, security, and typing gates.

## Scope and release policy

Only the current contracts are supported; the project does not maintain legacy
protocol or database compatibility guarantees. Unsafe or unqualified
boundaries fail closed.

Repository qualification uses immutable `v1.0.0-rc.*` Git tags. The Python
distribution remains `0.1.0` during this pre-publication phase, so Git RC tags
identify qualified source commits and are intentionally independent from the
package semantic version. Use the latest Git tag and its exact commit when
reproducing a candidate.

The repository does not claim universal distributed exactly-once execution, a
public control/A2A service, or a completed interactive Web/TUI product.
Operational release approval still requires evidence tied to the exact commit,
including a green CI run, backup/restore validation, transport governance, and
the intended soak/acceptance checks.
