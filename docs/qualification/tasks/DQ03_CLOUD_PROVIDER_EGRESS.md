# DQ03 — Cloud Provider and Egress Qualification

## Objective

Qualify the exact cloud-provider/account/model configuration used by Cloud Lead/Review calls and prove the local egress contract under real provider behavior.

## Required configuration record

Provider, endpoint, account/project, model identifier, SDK/client version, request timeout, retry policy, retention/training/privacy settings available to the account, region if applicable, structured-output mode and cost/token accounting source.

## Required scenarios

| ID | Scenario | Severity |
|---|---|---|
| DQ03-01 | exact non-secret provider configuration is persisted and immutable | HARD |
| DQ03-02 | PUBLIC and CLOUD_SAFE context succeeds | HARD |
| DQ03-03 | PROJECT_PRIVATE requires a new CLOUD_SAFE derived artifact | HARD |
| DQ03-04 | LOCAL_ONLY and SECRET never enter provider requests | HARD |
| DQ03-05 | nested structures and metadata are deterministically redacted | HARD |
| DQ03-06 | valid structured output succeeds and malformed output fails closed | HARD |
| DQ03-07 | timeout, 429 and 5xx retry behavior remains bounded | HARD |
| DQ03-08 | token and cost budget exhaustion fails closed | HARD |
| DQ03-09 | request cancellation becomes a durable terminal outcome | HARD |
| DQ03-10 | provider unavailability never triggers implicit fallback | HARD |
| DQ03-11 | accounting is attributable to Run, WorkOrder and Invocation | HARD |
| DQ03-12 | provider/model/endpoint/timeout/retry configuration drift is rejected before egress | HARD |

## HARD failures

Any sensitive egress, retry that bypasses policy/budget, provider switch without explicit policy, or accepted unvalidated structured output that can affect trusted workflow state.

## Exit criteria

The exact production-intended provider configuration has evidence. Sandbox/test-provider evidence alone is insufficient for Production Go.

## Executable matrix and evidence

Migration `0019` binds every Cloud Lead interaction to a credential-free
configuration snapshot covering provider, endpoint, account/project, region,
model, client, timeout, retry policy, retention/training/privacy settings,
structured-output mode and accounting sources. Cloud interactions atomically
create a required one-to-one `cloud_interaction_governance` record; other Agent
protocol interactions do not carry provider-only columns. Every governance
field is non-null, and a trigger prevents later mutation.

Run the fault matrix and retain its sanitized report outside the repository:

```bash
mkdir -p <evidence-root>/DQ03
DQ03_REPORT=<evidence-root>/DQ03/provider-egress-report.json \
  uv run pytest -q tests/qualification/test_dq03_provider_egress.py \
  --junitxml=<evidence-root>/DQ03/provider-egress-junit.xml
```

The repository matrix proves the software controls with deterministic test
providers. It does not satisfy the final exit criterion by itself: the exact
production-intended account configuration still requires separately produced
and independently accepted evidence.
