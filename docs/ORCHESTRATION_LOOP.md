# Orchestration Loop

The trusted controller drives a bounded sequence of durable transitions:

```text
NEW -> PLANNING -> ACTIVE -> POLICY_CHECK -> READY
    -> DISPATCHED -> EXECUTING -> VERIFYING -> REVIEW_READY
    -> REVIEWING -> ACCEPTED -> COMPLETED
```

Cloud calls occur only after `PLAN_REQUESTED` or `REVIEW_REQUESTED` has been committed. The Cloud Lead returns proposals and decisions; the controller persists them, evaluates policy, dispatches a typed WorkOrder, and invokes the independent verifier. A cloud `ACCEPT` is accepted only when the latest Attempt has a valid hard-pass VerificationResult and the review cites that verification ID.

## Revisions, retries, and evidence

Semantic changes create a new DRAFT WorkOrder with `parent_work_order_id` and a revision reason. The predecessor remains terminal and immutable. An unchanged execution failure can be retried through `retry_attempt`, which creates a new Attempt for the same WorkOrder; iteration accounting still applies. Verification failure is never converted into acceptance and is routed to a new revision.

Plans and ReviewDecisions are persisted as structured records. The final trace therefore links run, plan, WorkOrder lineage, Attempts, executor dispatch/result, artifacts/observations, VerificationResult, Cloud interaction, ReviewDecision, and terminal events.

## Pauses, approval, and cancellation

- Provider timeout/unavailability records `WAITING_EXTERNAL`; recovery resumes from durable state without asking a model what happened.
- Human review and pending capability approvals record `WAITING_HUMAN`. Only explicit `resolve_human`/`approve` controller commands resume them.
- Cancellation sets durable intent, prevents new dispatch, asks active execution backends to cancel, marks owned Attempts/WorkOrders cancelled, and preserves artifacts and audit history.

## Bounds and recovery

Each run has maximum iterations, cloud calls, and wall-budget configuration. Counters are persisted and checked before dispatch/model calls. `recover` reconciles an optional JobManager, records durable execution results found after restart, and then normal `advance` processes verification; no provider call is made by recovery itself.

`LocalControlAPI` exposes structured run/work-order status and append-only events for a loopback adapter or CLI. It does not serve raw artifacts, prompts, or agent chat, and no public service is started by the core package.
