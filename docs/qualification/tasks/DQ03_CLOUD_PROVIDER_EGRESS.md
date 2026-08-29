# DQ03 — Cloud Provider and Egress Qualification

## Objective

Qualify the exact cloud-provider/account/model configuration used by Cloud Lead/Review calls and prove the local egress contract under real provider behavior.

## Required configuration record

Provider, endpoint, account/project, model identifier, SDK/client version, request timeout, retry policy, retention/training/privacy settings available to the account, region if applicable, structured-output mode and cost/token accounting source.

## Required scenarios

- PUBLIC and CLOUD_SAFE context succeeds;
- PROJECT_PRIVATE raw artifact is denied unless transformed into a new permitted derived artifact;
- LOCAL_ONLY and SECRET never enter provider requests;
- nested structures/log metadata are redacted according to classification;
- structured output valid response;
- malformed structured output;
- provider timeout/429/5xx retry behavior;
- budget exhaustion;
- request cancellation where supported;
- provider unavailable returns typed failure and does not trigger implicit local-to-cloud or cloud-to-other-provider fallback;
- request/response accounting is attributable to the Run/WorkOrder/Invocation.

## HARD failures

Any sensitive egress, retry that bypasses policy/budget, provider switch without explicit policy, or accepted unvalidated structured output that can affect trusted workflow state.

## Exit criteria

The exact production-intended provider configuration has evidence. Sandbox/test-provider evidence alone is insufficient for Production Go.
