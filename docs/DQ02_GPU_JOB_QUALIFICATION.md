# DQ02 — GPU Admission and Long-Job Qualification

## Current contract

GPU is an optional deployment capability, not a prerequisite for the current
CPU/local-model control-plane workflow. DQ02 should be activated only when a
real GPU scheduler or container deployment is selected.

The V1 controller now treats GPU requests as an explicit resource contract:

- a GPU job requires a `GpuAdmissionController`;
- configured logical device IDs are leased exclusively and durably in SQLite;
- the trusted controller injects the leased device IDs into the typed backend
  submission contract;
- a second job cannot acquire an occupied device;
- releasing a terminal job makes the device available again;
- a backend that cannot enforce the assigned device must fail closed.

This is **admission control**, not proof of hardware isolation. A scheduler or
container runtime must enforce the returned device assignment before production
can claim GPU isolation.

## Implemented evidence

- Migration `0008` persists `gpu_leases` with job/device/state records.
- `GpuAdmissionController` supports acquire, active inspection, release, and
  restart observation from a newly constructed controller. Its reconciliation
  pass releases only leases whose Job is known terminal, leaving `LOST` and
  non-terminal work occupied.
- `JobManager` rejects GPU submission without an admission controller and
  releases leases after backend submission failure or known terminal
  reconciliation. `LOST` retains its lease until an operator calls the explicit
  lost-job resolution path.
- A GPU `JobSpec` must carry a positive `max_gpu_seconds` budget; zero-budget
  GPU requests are rejected before submission.
- Regression tests cover exclusive allocation, persistence, release, and
  fail-closed submission. The local durable backend still rejects GPU jobs by
  design because it has no hardware enforcement.

## Pending target-environment qualification

The following cannot be proven by the current CPU/WSL test environment:

- authorized device visibility and unauthorized GPU invisibility;
- exclusive GPU ownership against vLLM and other workloads;
- OOM cleanup, cancellation, lease release, and controller-crash recovery;
- scheduler-side correlation and reconciliation across the C1–C6 crash windows;
- driver/CUDA/container-toolkit compatibility and GPU monitoring semantics.

`LOST` is intentionally a hold state: automatic release would allow a second
job to reuse a device while the first native job might still be running. An
operator must inspect the scheduler and explicitly resolve the lost job before
its lease is released.

Until these are demonstrated on the selected scheduler/container deployment,
the release status remains `Deployment Qualification Pending`.
