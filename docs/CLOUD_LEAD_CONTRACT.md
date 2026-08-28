# Cloud Lead Contract

## Trust boundary

Cloud Lead is advisory and outbound-only. Its public facade accepts authoritative `CloudContextSelection` IDs, reconstructs an immutable safe bundle from SQLite, and sends one stateless request. It has no filesystem, shell, capability broker, Artifact upload, MCP, A2A, hosted tool, conversation, or local-execution interface. Controller state, deterministic policy, and the verifier remain authoritative.

The configured provider adapter uses direct HTTPS with an exact host allowlist, a nonempty bearer credential, environment proxy trust disabled for owned clients, finite timeout, and a transport response-byte ceiling. URLs containing credentials, query strings, or fragments are rejected. API-key storage and process isolation are deployment responsibilities.

## Structured operations

The facade exposes four typed operations:

- `propose_plan` returns `PlanProposal`.
- `propose_work_order` returns `WorkOrderProposal`.
- `request_evidence` returns `EvidenceRequest`.
- `review` returns `ReviewDecision` with evidence references.

Provider output is JSON validated by Pydantic. Duplicate proposal/capability IDs and malformed fields fail validation. Invalid output receives only a bounded schema-error repair instruction; the previous raw response is neither echoed nor persisted. Exhaustion becomes `CLOUD_SCHEMA_INVALID`.

## Budgets and lifecycle

Every interaction has request-count, input-byte, response-byte, output-token, and aggregate-token limits. Reported token usage is accumulated across repair attempts using at least prompt plus completion tokens, and cost is calculated from configured pricing. The interaction stores its bundle hash, provider/model, purpose, structured successful response, request ID, attempts, tokens, cost, status, and reason code. Started/finished audit events bracket it.

Provider timeout or non-retryable unavailability produces the explicit `WAITING_EXTERNAL` interaction state with `CLOUD_UNAVAILABLE`; bounded classified transient failures may retry within the interaction request budget using capped backoff. It is never interpreted as model rejection or local fallback. TASK 06 orchestration still decides run-level retry/resume behavior.

## Non-authority guarantees

A proposed forbidden capability remains data until deterministic policy denies it. A cloud `ACCEPT` recommendation cannot transition a WorkOrder past failed or stale hard verification. Cloud output cannot mutate controller state directly.

## Tracing and retention

The baseline deliberately avoids provider SDKs and SDK tracing. Requests set streaming and provider storage off and omit tools/conversation identifiers. Only the canonical safe-bundle hash and validated successful output are persisted; malformed raw provider text is not retained. Provider-side logging/retention must additionally be disabled or governed in deployment configuration.
