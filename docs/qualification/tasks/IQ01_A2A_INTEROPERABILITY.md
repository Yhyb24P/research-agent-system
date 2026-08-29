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

## Exit criteria

All HARD checks pass against at least one independent implementation. The same scenario suite must pass twice from a clean controller state with no unexplained authoritative state differences.

## Forbidden claim before pass

Do not claim "A2A production interoperable" or "A2A conformance qualified".
