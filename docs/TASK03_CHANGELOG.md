# TASK 03 Changelog

## Baseline

TASK 02 Gate passed with content-addressed artifacts, immutable classifications, complete/atomic derivation provenance, deterministic policy, hash-bound approvals, and cloud-egress negative tests.

## Files changed

- Added typed executor/model/sandbox/job contracts.
- Added BubblewrapBackend with network/filesystem/environment/process/resource controls.
- Added persistent Capability Broker operation idempotency and bounded output artifacts.
- Added persistent Attempt dispatch idempotency and structured LocalExecutorWorker.
- Added loopback-only vLLM-compatible LocalModel adapter with no fallback.
- Added unique Git worktree manager and persisted worktree provenance.
- Added operation-ID durable local Job backend, detached runner, JobManager, restart reconciliation, cancellation, and events.
- Added migration `0003`, real sandbox security tests, fake-model/worktree/pytest integration tests, and Job crash-window tests.

## Domain/API changes

Added executor DTOs for sandbox commands/results, capabilities, granted work, local model requests/responses, executor results, and typed Jobs. Added LocalModel, SandboxBackend, and JobBackend protocols. Added persistent `execution_steps`, `executor_dispatches`, and `attempt_worktrees` records.

## Migration changes

Revision `0003` adds execution-step and Attempt-dispatch idempotency records plus persisted worktree provenance.

## Security impact

- No host generic shell exists for agents.
- Actual bubblewrap namespaces hide host roots/secrets and remove networking.
- Host environment is cleared; local model endpoint is loopback-only and ignores proxy environment by default.
- Timeouts/output limits/cancellation terminate the sandbox process tree.
- Worktree and operation IDs prevent dirty/repeated side effects.
- Long Job states and audit events remain DB-authoritative and restart-reconcilable.
- No Cloud Lead, cloud fallback, A2A, MCP, Git push, or system package installation was added.

## Tests executed / results

- `uv run pytest`: 51 passed, including real bubblewrap, Git worktree, fake local model, pytest-in-sandbox, Job restart/cancellation, and crash-window tests.
- `uv run mypy`: strict mode succeeded across 63 source/test files.
- `uv lock --check`: dependency lock is current.
- `uv run alembic heads/history`: one linear head at `0003`; migrated-schema model drift checks pass.
- A2A/MCP/LangGraph/Anthropic import scan across runtime source: no matches.

## Known limitations

See `EXECUTOR_SECURITY.md`: aggregate workspace disk/file-count quotas, GPU isolation, non-none networking, non-Linux portability, and adversarial filesystem race hardening are not claimed. The local Job backend provides crash-safe at-most-one operation-directory launch, not distributed exact-once guarantees.

## Deferred work

Independent deterministic verification remains TASK 04. Cloud Lead/model context calls, orchestrated loop, protocol adapters, and hardening/pilot remain later Gates.

## Gate checklist

- [x] Traversal and symlink escape blocked.
- [x] Curl fails with no routable sandbox interface.
- [x] Host secret environment fixture unavailable.
- [x] Timeout/cancel kills descendants; output/resource caps enforced.
- [x] Capability and Attempt dispatch duplicates reuse persisted results.
- [x] Job duplicate operation returns one Job and restart/crash-window reconciliation does not resubmit.
- [x] Local model outage has no cloud fallback.
- [x] Dirty worktree is never reused and provenance is persisted.
- [x] Fake WorkOrder modifies only isolated worktree and runs pytest.
- [x] Full security/recovery/regression/static/migration checks pass.
