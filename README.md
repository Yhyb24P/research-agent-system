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
- Durable `RuntimeSession` instances and typed START/ATTACH/STOP command
  receipts, with PROCESS and REMOTE_HTTP supervision, strong process identity,
  restart reconciliation, and no direct Agent authority over lifecycle state.
- A fail-closed `researchd` startup barrier that orders schema/storage checks,
  workspace/worktree recovery, runtime/job/invocation reconciliation, and audit
  stream validation before any typed mutation may be dispatched.
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
- Loopback JSON control API, Browser Control Tower, static TUI renderer, SQLite backup/restore checks,
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

Run the real-process A2A interoperability qualification matrix with the `a2a`
extra installed:

```bash
uv run pytest -q tests/qualification/test_iq01_real_interoperability.py
```

Run the Agent runtime and invocation lifecycle qualification matrix:

```bash
uv run pytest -q tests/qualification/test_dq02_runtime_lifecycle.py
```

Run the provider configuration and egress-governance software matrix:

```bash
uv run pytest -q tests/qualification/test_dq03_provider_egress.py
```

Run the backup/restore/DR software matrix:

```bash
uv run pytest -q tests/qualification/test_dq04_backup_restore.py
```

Current-format backups require an explicit immutable candidate commit and RC
tag. Restore also requires the independently expected commit and tag; old
snapshot formats and old database schemas are intentionally rejected rather
than upgraded in place. The software matrix does not replace the actual
off-host and primary-loss drill required for DQ04 acceptance.

Integration tests are the executable reference workflow. The repository now
ships the concrete `researchd` composition root and CLI around the durable
RuntimeSession/Supervisor services. The daily `research` client now covers
the lifecycle surface (`init`, `status`, interactive entry) and the first
shell command batch (`status`, agent working set, `run list`,
`task create`/`task cancel`, `msg`, `events watch`, `approve`, `reject`);
the Browser Control Tower opens with `research browser`.

An embedding composition must register its trusted services and use
`build_startup_barrier(...)`. The barrier verifies migration `0025` and live
DB/CAS state, then invokes the existing workspace, worktree, RuntimeSession,
job, and invocation recovery paths in the frozen order. A failed or skipped
phase leaves `ResearchDaemon` non-ready; callers cannot bypass that state with
a free-text or direct-SQL mutation.

Qualify the actual deployment filesystem rather than a temporary substitute:

```bash
uv run python scripts/dq01_preflight.py --strict --target <deployment-root>
uv run python scripts/dq01_filesystem_probe.py --root <deployment-root>
```

## Inspect the control plane

Create one strict configuration file. Paths must be absolute; repositories are
identified Git roots and job types map to fixed argv arrays, never shell text:

```bash
cat > researchd.json <<'JSON'
{
  "database": "/absolute/path/researchd.db",
  "artifact_root": "/absolute/path/artifacts",
  "state_root": "/absolute/path/state",
  "repositories": {"main": "/absolute/path/source-repository"},
  "job_commands": {"typed-check": {"argv": ["/usr/bin/true"]}},
  "host": "127.0.0.1",
  "port": 8788
}
JSON
uv run researchd --config researchd.json validate
uv run researchd --config researchd.json inspect
uv run researchd --config researchd.json init
uv run researchd --config researchd.json serve
curl http://127.0.0.1:8788/api/health
TOKEN=$(tr -d '\n' < /absolute/path/state/control.token)
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8788/api/runs
```

`serve` never migrates an existing database. It reaches READY only after the
frozen eight-stage recovery barrier passes; a non-current schema, missing
repository, unknown configuration field, relative path, or non-loopback bind
fails closed. An empty `job_commands` map intentionally disables job submission.
`validate` parses the contract without touching state. `inspect` additionally
prints its SHA256 and a non-secret projection: fixed command arguments are
represented only by their count and are never echoed.

`init` creates a fresh 256-bit credential at `<state_root>/control.token` with
mode `0600` and refuses to replace one. `serve` refuses missing, malformed,
non-owner or incorrectly permissioned credentials. `/api/health` remains the
unauthenticated liveness/readiness surface; all other HTTP reads,
streams and mutations require `Authorization: Bearer <token>`. The credential
is never stored in SQLite, audit metadata, snapshots, configuration inspection
or Agent context.

If recovery leaves an unresolved RuntimeSession, workspace, worktree, job, or
invocation, `researchd` remains running only to expose its non-ready health and
read projections. Every daemon-routed mutation is rejected until the unsafe
state is resolved.

`researchctl` opens an existing database without constructing an orchestrator:

```bash
uv run researchctl --database researchd.db run list
uv run researchctl --database researchd.db agent list
uv run researchctl --database researchd.db events <run-id> --after <stream-offset>
uv run researchctl --database researchd.db daemon-command list --status ACCEPTED
uv run researchctl --database researchd.db daemon-command resolve <command-id> --resource-ref run_id=<run-id>
```

`daemon-command resolve` converges an interrupted `ACCEPTED` receipt through
the same command-specific observation as the HTTP route; add `--abandon` to
abandon an undetermined outcome and `--command-id` for idempotent retries.

The daily `research` client drives the same control plane over the
authenticated HTTP surface and never opens the database:

```bash
uv run research --config researchd.json init
uv run research --config researchd.json status
uv run research --config researchd.json browser
uv run research --config researchd.json
```

`init` delegates bootstrap to `researchd init`; `status` prints one JSON
document with reachability and readiness. Without a subcommand, `research`
probes the daemon and, when none is reachable, spawns `researchd serve` as a
controller process that remains independent when the interactive shell exits.
The shell is entered only after the daemon reports READY — a non-ready daemon is
surfaced with its failed startup phase, never bypassed. The first shell
batch offers `status`, `agent list` / `agent use` / `agent remove`,
`run list`, `task create`, `task cancel`, `msg`, `handoff list` /
`handoff accept` / `handoff reject`, `events watch`, `approve`, `reject` and
`remote attach <runtime-id>` / `remote renew <runtime-id>` / `remote detach <runtime-id>`, and `quit`; every command crosses the authenticated
transport, and `agent remove` only clears the session-local working set.

`research browser` opens a loopback-only Browser Control Tower. The HTML, CSS,
and JavaScript contain neither controller state nor credential. The daily
client places the already-local credential in the URL fragment (which is never
sent in HTTP), and the page immediately removes it while retaining it only in
memory. Every read and typed command still crosses the same authenticated
control API; browser layout and refresh state are never authoritative.
It presents the run-scoped Collaboration Window, per-Agent Console, and a
system-event stream that reconnects from its in-memory offset.

Remote attachment is distinct from a local runtime session. It can only attach
an installed A2A runtime by ID; the daemon resolves its registered endpoint,
protocol and tenant, owns the renewable runtime lease, and never accepts those
values from the client.

Managed PROCESS Agent invocation is resolved dynamically through the installed
Agent catalog; no Agent ID is fixed in the daemon composition. An executor is
eligible only with an active runtime lease and a HEALTHY RuntimeSession whose
launch-profile hash still matches the trusted catalog. Invocation uses the
runtime's registry-owned loopback endpoint and never starts the launch command
a second time.

The credential-free managed coder installation example is
`examples/managed_coder_agent_definition.example.json`. Its absolute executable
and working directory are deployment-owned values and must be deliberately
adapted before installation; clients never submit them through the runtime API.
The `research-coder-agent` reference process implements the corresponding
credential-free loopback turn protocol. It proposes typed actions only;
`researchd` executes granted actions through `CapabilityBroker` and constructs
the authoritative `ExecutorResult`.

Collaboration messages use a closed purpose vocabulary (`DISCUSSION`,
`STATUS`, `QUESTION`, `DIRECTIVE`, `NOTICE`) and may durably reference one
WorkOrder, Delegation, Invocation, or prior message in the same run. These
links remain communication context and never confer workflow authority.
The authenticated `msg` command may add `--reply-to`, `--delegation`, or
`--invocation` links. Sender identity is absent from the request schema and is
bound by the daemon as the authenticated local human.
Native collaboration reads are available by message ID and at
`GET /api/runs/<run_id>/messages`; the run timeline carries the same durable
reply/delegation/invocation links. `LOCAL_ONLY` and `SECRET` bodies are
redacted from these presentation projections.

Install the optional `tui` extra and run `research --config researchd.json tui`
for the projection-only workspace with Collab, Agents, Tasks, Approvals and
System tabs. Refresh and layout state stay client-local; the TUI has no direct
database or business-logic path.

The same daemon can be observed through independent terminal clients:

```bash
research --config researchd.json console collab --run run_example
research --config researchd.json console agent agent_coder --run run_example
research --config researchd.json console system
```

Each console is a disposable projection client; quitting one never stops an
Agent, a runtime session, or the daemon.

Managed turn requests carry the canonical invocation purpose and a structured
payload; execution turns receive the controller-built local request, while
planning and review turns return a structured business output validated by the
controller. Agent-origin messages and handoff proposals enter through
`AgentActionBroker`. Their actions contain no caller-supplied authority scope:
the broker derives sender, run, WorkOrder, Delegation, Agent and runtime from
the live Invocation and rejects stale leases or ownership. A handoff is only
valid from an execution-scoped invocation.

Handoff is a separate non-authoritative `HandoffProposal`, never message text.
An Agent may propose `CONTINUE` or `REVISE` through the same invocation-bound
broker; source identity and scope are derived, and all referenced artifacts or
observations must belong to that run. Only the Controller may accept it.

For a deliberately read-only projection without daemon mutations, embed only
the local API:

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
curl http://127.0.0.1:8788/api/runtime-sessions
curl http://127.0.0.1:8788/api/daemon-commands
curl http://127.0.0.1:8788/api/system-events?after=0
curl http://127.0.0.1:8788/api/collaboration-messages/<message-id>
curl -N http://127.0.0.1:8788/api/runs/<run-id>/stream?follow=1
curl -N http://127.0.0.1:8788/api/system-stream?follow=1
```

Run event payloads carry `actor_type`/`actor_id` alongside the stream
offset, and the system stream (`/api/system-stream`, SSE with
`Last-Event-ID`/`after` resume) mirrors the JSON
`/api/system-events` projection over the same monotonic audit stream.
Reading a collaboration message by id returns the stored record including
its classification; the AG-UI stream keeps redacting `LOCAL_ONLY` and
`SECRET` bodies regardless.

The read-only bootstrap intentionally rejects state-changing commands. An
embedding application must construct `LocalControlAPI` with a
`ResearchOrchestrator`; typed commands then cross the `ResearchDaemon`
readiness gate and enter the existing policy/state machine through these
routes. An external request carries `request_version`, `command_id`, and only
route-specific intent fields. It cannot submit `actor_type` or `actor_id`;
the HTTP adapter binds a HUMAN identity server-side before constructing the
internal command. An accepted dispatch returns `202` with the versioned
`DaemonCommandResult` envelope (`command_version`,
`command_id`, `command_type`, `status`, `resource`):

| Method | Route | Route-specific body fields |
|---|---|---|
| `POST` | `/api/agents/{agent_id}/start` | `runtime_id` (optional) |
| `POST` | `/api/runtime-sessions/{runtime_session_id}/stop` | `runtime_id`, `expected_version` |
| `POST` | `/api/runs/{run_id}/cancel` | — |
| `POST` | `/api/work-orders/{work_order_id}/approve` | `grant_id` |
| `POST` | `/api/work-orders/{work_order_id}/human-decision` | `action` (`abort` or `revise`; `revise` requires `objective`) |
| `POST` | `/api/work-orders/{work_order_id}/reject` | `approval_id` |
| `POST` | `/api/workspaces` | `workspace_id`, `name` |
| `POST` | `/api/runs` | `workspace_id`, `objective`, `run_id` (optional) |
| `POST` | `/api/collaboration-messages` | `message_id` (`msg_…`), `run_id`, `purpose`, `body`, `recipient_agent_id` (optional, `agent_…`), `classification` (optional, default `PROJECT_PRIVATE`) |
| `POST` | `/api/backups/create` | `destination`, `candidate_commit` (40-hex), `candidate_tag` (`vX.Y.Z-rc.…`) |
| `POST` | `/api/backups/verify` | `snapshot` |
| `POST` | `/api/restores/plan` | `snapshot`, `database_destination`, `artifact_destination`, `expected_candidate_commit`, `expected_candidate_tag` |
| `POST` | `/api/daemon-commands/{command_id}/resolve` | `resource_ref` (family-specific key/value pairs), `abandon` (optional) |

`POST /api/agents/{agent_id}/start` is the only public launch route: it
accepts just an optional `runtime_id` and the daemon resolves the launch spec
from the trusted launch catalog (PROCESS runtimes start a supervised process
session, HTTP runtimes attach to the registered endpoint), deriving the
runtime session identity from the command identity so a replayed command maps
to the same session. The former arbitrary
`/api/runtime-sessions/start` and `/api/runtime-sessions/attach` routes are
disabled; stopping a session still uses the per-session stop route above.

The workspace, research-task, reject, and collaboration-message routes run
through the orchestrator control authority, so they require the embedding
application to expose a `ResearchOrchestrator`; without one they fail closed.
`POST /api/work-orders/{work_order_id}/reject` converges a `WAITING_APPROVAL`
order and its pending approval to `FAILED`/`REJECTED` (the run fails with
`APPROVAL_REJECTED`), symmetric with a policy denial. The three backup routes
are bound to the daemon's own database and artifact root: create produces an
atomic snapshot tree, verify validates it without copying, and the restore
plan is a dry run that never writes. Because verify and plan leave no
persistent effect, an interrupted receipt for either can only be abandoned
(`OPERATOR_ABANDONED`), never asserted complete.

Migration `0021` reserves a durable generic receipt before dispatch. Reusing
the same command identity and request replays its completed or rejected result
without repeating the side effect; reusing the identity with different input
is rejected. A receipt left `ACCEPTED` by an interrupted dispatch is never
replayed automatically: it remains visible through `/api/daemon-commands` and
blocks READY until the operator converges it. The recovery channel is
`POST /api/daemon-commands/{command_id}/resolve` (or `researchctl
daemon-command resolve`): a command-specific observer first observes the
family's authoritative state, and the operator may only abandon an
undetermined outcome (`OPERATOR_ABANDONED`) — there is no free-form terminal
override. The target receipt, the resolution receipt and the audit events
commit in one transaction, and a terminal target can only be replayed, never
re-resolved. The route stays reachable while the daemon is FAILED, but still
requires the Bearer token.

The local token authenticates the owner client while server-side actor binding
prevents payload attribution. This is the PX00 MVP boundary; later native peer
credentials may replace the token without changing internal command authority.

Migration `0022` adds a one-to-one, server-owned `RuntimeLaunchProfile` for an
existing `AgentRuntime`. Public start/attach requests contain no executable,
argv, cwd, endpoint, or health override. The daemon resolves the enabled
profile, verifies its canonical digest, constructs the internal command, and
persists both the resolved launch-spec snapshot and profile hash on the
RuntimeSession. Until PX01 supplies the installation command, profiles are
registered only through the trusted in-process configuration service; a
missing, disabled, wrong-mode, or tampered profile fails closed.

Arbitrary UI events have no mutation endpoint.

## Repository map

- `src/researchd/collaboration/`: Agent contracts, registry, selection,
  delegation, invocation, adapters, messages, and LangGraph runtime.
- `src/researchd/workspace/`: workspace grants, admission, transport, lease,
  reconciliation, and cleanup.
- `src/researchd/orchestrator/`: trusted bounded workflow controller.
- `src/researchd/runtime_sessions/` and `supervisor/`: durable concrete
  Agent-runtime instances, typed command receipts, side-effect drivers, and
  restart reconciliation.
- `src/researchd/daemon/`: startup recovery barrier and readiness-gated typed
  mutation boundary.
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

The repository does not claim universal distributed exactly-once execution or a
public control/A2A service.
Operational release approval still requires evidence tied to the exact commit,
including a green CI run, backup/restore validation, transport governance, and
the intended soak/acceptance checks.
