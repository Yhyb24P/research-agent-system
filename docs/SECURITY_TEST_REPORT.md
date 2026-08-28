# Security and Recovery Test Report

## Execution

The complete repository regression suite was run with `.venv/bin/pytest`; the suite includes the A01–A30 acceptance scenarios, security sandbox tests, cloud/vLLM timeout injection, controller restart/reconciliation, approval mutation/replay, artifact corruption, duplicate Job submission, A2A/MCP boundary tests, and the bounded pilot. Strict mypy, lock, and fresh Alembic upgrade/check were also run.

## Acceptance matrix evidence

| IDs | Evidence | Result |
|---|---|---|
| A01–A03 | `tests/security/test_sandbox.py`, `tests/integration/test_executor.py` | pass; host paths/secrets/network denied and local outage has no cloud fallback |
| A04–A05, A19, A25 | `tests/security/test_task02_security.py`, `tests/integration/test_cloud_lead.py` | pass; classification, derivation, redaction, and unknown-class fail closed |
| A06–A07, A20, A24 | `tests/integration/test_verifier.py`, `test_cloud_lead.py` | pass; independent verifier and bounded cloud repair |
| A08–A10, A22–A23 | `tests/integration/test_executor.py`, `tests/security/test_sandbox.py` | pass; operation dedup, restart reconciliation, cancellation, quotas, and policy budgets |
| A11–A12, A26, A30 | `tests/integration/test_orchestrator.py`, `test_hardening.py` | pass; revision lineage, Attempt retry API, terminal immutability, complete pilot trace |
| A13–A18, A21 | `tests/security/test_task02_security.py`, `tests/integration/test_storage.py` | pass; hashes, approvals, concurrency and deterministic policy |
| A27 | `tests/integration/test_protocol_adapters.py` | pass; terminal A2A refinement creates a new mapping |
| A28–A29 | `tests/integration/test_protocol_adapters.py` | pass; invalid Origin and non-loopback HTTP bind rejected |

## Fault injection and recovery

Cloud provider timeout and transport-size failures, vLLM timeout, malformed model output, DB event rollback, artifact corruption, Job crash-before-handle-update, duplicate operation, controller restart after persisted execution, and cancellation are covered. Fault points are available through `researchd.testing.FaultInjector` for deployment-specific integration tests.

## Release blockers / open risks

The following are not falsely claimed solved by unit tests and remain deployment release blockers until demonstrated in the target environment: GPU isolation, scheduler exactly-once/at-least-once behavior, filesystem mount/symlink race resistance, provider-side retention/account settings, WSL/container runtime variance, and tested off-host backup/restore procedures. Core V1 remains safe by failing closed or requiring explicit operator action at these boundaries.
