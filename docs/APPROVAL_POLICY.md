# Approval Policy

Dangerous or expensive operations require an auditable grant bound to exact canonical parameters. An LLM may request an operation but cannot approve it or alter the approved scope.

## Canonical binding

The controller serializes this object as UTF-8 JSON with sorted keys, compact separators, Unicode preserved, and NaN/Infinity rejected:

```json
{"operation_type":"git.push","parameters":{"branch":"main","remote":"origin"}}
```

`parameter_sha256` is the SHA256 of those canonical bytes. Object key order therefore does not affect approval identity; any material value or operation-type change does.

## Lifecycle

1. `ApprovalService.request` records operation, canonical parameters/hash, requester, reason, risk, resource scope, budget delta, expiry, and one-shot policy.
2. A user creates a grant whose expiry cannot exceed the request expiry.
3. `authorize` recomputes the requested operation hash and compares it to both request and grant.
4. Expired, mismatched, missing, rejected, or replayed grants fail closed.
5. A one-shot grant is consumed with a conditional database update (`used_at IS NULL` and unexpired), so concurrent replays cannot both succeed.

Reusable grants must be explicitly requested with `one_shot=false`; they remain parameter- and TTL-bound.

## Policy interaction

The deterministic Policy Engine intersects requested capabilities with workspace and user allowlists, enforces every budget ceiling and classification rule, and removes capabilities awaiting approval. It produces stable outcomes and reason codes:

- `ALLOW`
- `DENY`
- `APPROVAL_REQUIRED`

Policy decisions can be persisted with the run/work-order IDs, requested/effective capabilities, outcome, reason codes, and timestamp. Agent explanations are not policy authority.
