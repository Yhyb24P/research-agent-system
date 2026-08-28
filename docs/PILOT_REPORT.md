# Bounded Pilot Report

## Workload

The pilot uses a temporary real Git repository containing a deliberately failing `calc.py` test. A fake local model is used only to provide deterministic bounded intent; the actual `WorktreeManager`, Bubblewrap sandbox, `CapabilityBroker`, content-addressed artifact service, controller orchestrator, verifier, and Cloud Lead context/review path are exercised.

The local worker writes the smallest correction (`a - b` → `a + b`) inside a fresh detached worktree and runs the focused pytest target with network disabled. The source repository remains unchanged. The verifier evaluates the structured test result independently of the worker claim; the Cloud Lead receives only the safe structured verification packet and returns a typed review.

## Result

The pilot reached `COMPLETED` with one Attempt and one accepted WorkOrder. The persisted trace includes Goal/ResearchRun, PLAN_REQUESTED/PLAN_CREATED, WorkOrder and policy decision, dispatch/Attempt, worktree and executor result, VerificationResult, Cloud interaction, ReviewDecision, WORK_ORDER_ACCEPTED, and RUN_COMPLETED. The test asserts the trace and source/worktree separation.

## Boundary

This is a bounded control-plane pilot, not a production scientific campaign. It does not claim target-environment GPU, scheduler, provider retention, or multi-host guarantees. Those remain explicit TASK08 release blockers in `KNOWN_LIMITATIONS.md`.
