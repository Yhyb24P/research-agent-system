# IQ03 — AG-UI Replay and Reconnect Qualification

## Objective

Prove that the presentation event plane is resumable, read-only, ordered and non-leaking.

## Required scenarios

| ID | Scenario | Severity |
|---|---|---|
| IQ03-01 | snapshot followed by replay from durable offset | HARD |
| IQ03-02 | disconnect and reconnect with `Last-Event-ID` | HARD |
| IQ03-03 | no missing semantic event across reconnect | HARD |
| IQ03-04 | replay does not duplicate client-applied semantic transition | HARD |
| IQ03-05 | controller restart preserves monotonic offsets | HARD |
| IQ03-06 | LOCAL_ONLY collaboration text is redacted | HARD |
| IQ03-07 | SECRET collaboration text is redacted | HARD |
| IQ03-08 | arbitrary AG-UI input cannot mutate trusted state | HARD |
| IQ03-09 | simultaneous typed commands use authoritative concurrency semantics | MAJOR |
| IQ03-10 | high-volume timeline remains bounded enough for intended UI client | MAJOR |

## Test method

Use a deterministic fixture Run containing workflow, approval, workspace, verification and human-message events. Compare the canonical audit stream to the client-visible projected sequence. Client-side reconnect tests must start from recorded stream offsets rather than wall-clock timestamps.

The current intended-client load profile is 2,000 durable events in one replay:
offsets must be exactly contiguous and unique, and the serialized SSE payload
must remain below 2 MB. Simultaneous approval commands must exercise the real
one-shot approval authority; exactly one command may consume the grant.

## Evidence

Store canonical audit offsets, projected event types, reconnect cursor, duplicate/missing count, redaction assertions and command-response status codes.

## Exit criteria

For HARD checks: missing events = 0, unauthorized mutations = 0, classified payload leaks = 0, and cursor ordering violations = 0.
