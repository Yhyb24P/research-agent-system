# DQ03 — Cloud Lead Qualification

Cloud access is an optional integration, not a prerequisite for the local
control plane. Qualification must use synthetic or PUBLIC/CLOUD_SAFE fixtures;
SECRET, LOCAL_ONLY, private paths, and real credentials must never be used as
provider test data.

## Required staging evidence

Record the provider, account/project, model, region, endpoint, tested date,
credential reference (never its value), retention/training policy, and SDK or
transport version. The result is valid only for that configuration and date.

| Scenario | Required observation | Current code evidence |
|---|---|---|
| Safe bundle | Only authoritative redacted context is sent | `test_cloud_mock_receives_only_safe_redacted_authoritative_bundle` |
| First-pass schema | Plan/work-order/review DTO validates without repair | Cloud Lead integration tests |
| Malformed output | Bounded repair attempts, then `CLOUD_SCHEMA_INVALID` | malformed-output test |
| Timeout/unavailable | `WAITING_EXTERNAL` and no local/cloud fallback | provider-timeout test |
| Response limit | Oversized response fails closed | Cloud Lead integration tests |
| Token/cost budget | All attempts accumulate usage and cost | repair/accounting test |
| 429/5xx/rate limit | Bounded provider-specific behavior and audit trail | Retry classification/backoff unit evidence; **pending real staging** |
| Retention/training | Account and endpoint policy recorded and reviewed | **pending deployment** |
| Model drift | Repeated canary run preserves schema and semantic review gates | **pending staging** |

JSON-schema validity is not research correctness. A successful provider call
still requires the independent verifier and policy/controller gates before a
run can complete.

## Canary and retry rules

Use a deterministic PUBLIC canary and a separate LOCAL_ONLY non-egress canary.
The provider must observe zero bytes from the latter. Retry only bounded,
classified transient failures; every attempt must retain request ID, provider,
model, bundle hash, status, reason code, token count, and cost. Never use an
unbounded `while failure` loop, and never retry by silently changing the
context classification or provider.

The current implementation bounds total Cloud Lead requests and schema repair,
classifies transient provider failures, applies capped exponential backoff (or
the provider's bounded `Retry-After`), records accounting, and fails closed on
unavailable providers. A production retry/backoff policy remains a deployment
decision until a real provider's 429/5xx behavior and cost limits are measured.
