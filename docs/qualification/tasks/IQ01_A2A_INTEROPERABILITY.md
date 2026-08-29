# IQ01 — A2A Real Interoperability

## Objective

Prove that the researchd A2A v1 boundary interoperates with an independent A2A implementation without allowing protocol state to replace authoritative researchd state.

## Preconditions

- exact candidate commit frozen;
- `a2a-sdk` lock resolved and recorded;
- at least one independent server implementation/environment available;
- test Agent Card and tenant configuration recorded;
- external service contains no production secret or sensitive research data.

## Required scenarios

| ID | Scenario | Severity |
|---|---|---|
| IQ01-01 | Agent Card discovery/parse and supported interface selection | HARD |
| IQ01-02 | typed GrantedWorkOrder message round trip | HARD |
| IQ01-03 | completed task with exactly one typed ExecutorResult artifact | HARD |
| IQ01-04 | streaming status/artifact aggregation preserves order and scope | HARD |
| IQ01-05 | task list/get maps to adapter state only, not domain ownership | HARD |
| IQ01-06 | cancel remote task and reconcile local invocation outcome | HARD |
| IQ01-07 | tenant propagation/isolation | HARD |
| IQ01-08 | AUTH_REQUIRED is preserved and does not become implicit approval | HARD |
| IQ01-09 | wrong `attempt_id` is rejected fail-closed | HARD |
| IQ01-10 | malformed/duplicate typed output artifact is rejected | HARD |
| IQ01-11 | disconnect/reconnect does not create a second authoritative attempt | MAJOR |
| IQ01-12 | duplicate dispatch exercises idempotency/reconciliation | HARD |

## Implementation work allowed

Only compatibility fixes, codecs, adapter guards, tests, fixtures and qualification helpers needed to satisfy the current contract. Do not redesign ResearchRun/WorkOrder/Attempt semantics to accommodate remote protocol behavior.

## Evidence

Capture independent implementation identity/version, Agent Card, sanitized protocol traces, researchd invocation/task mapping, audit offsets, final Attempt state, result artifact hash and all negative-test rejections.

The executable boundary is `tests/qualification/independent_a2a_agent.py`. It
runs as a separate process using the official A2A server SDK and does not import
`researchd`. The controller side uses `OfficialA2AClient`; therefore the test
crosses Agent Card discovery, HTTP/JSON-RPC transport, streaming, task storage,
tenant isolation, cancellation and controller reconciliation rather than
calling an in-process fake.

Run the matrix twice from clean Agent and controller state, retain the
sanitized report outside the source repository, and capture JUnit separately:

```bash
mkdir -p <evidence-root>/IQ01
IQ01_REPORT=<evidence-root>/IQ01/interoperability-report.json \
  uv run pytest -q tests/qualification/test_iq01_real_interoperability.py \
  --junitxml=<evidence-root>/IQ01/interoperability-junit.xml
```

The report contains no request body beyond the bounded qualification fixture.
It records both cycle summaries, Agent Cards, official SDK version, server
script hash, sanitized stream/server traces, authoritative mapping, audit
offsets, post-reconciliation Attempt state and result hash. A test pass is an
observation only: the report still needs an evidence envelope bound to the
exact candidate and independent acceptance under `GATE_POLICY.md`.

## Exit criteria

All HARD checks pass against at least one independent implementation. The same scenario suite must pass twice from a clean controller state with no unexplained authoritative state differences.

## Forbidden claim before pass

Do not claim "A2A production interoperable" or "A2A conformance qualified".
