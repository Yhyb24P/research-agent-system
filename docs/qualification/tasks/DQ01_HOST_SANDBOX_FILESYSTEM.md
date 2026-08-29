# DQ01 — Host, Sandbox and Filesystem Qualification

## Objective

Qualify the actual deployment host for sandbox containment, filesystem semantics and process cleanup.

## Target fingerprint

Record kernel/OS, Python, Bubblewrap, filesystem type/mount options, WSL/native/container status, user namespace settings, cgroup availability, interpreter path, Git version and relevant security controls.

## Required checks

- Bubblewrap runs unprivileged with expected capability posture.
- sandbox has no network by default;
- only declared mounts are visible;
- writable paths are bounded to the attempt/work area;
- symlink/hardlink/path traversal cannot escape allowed roots;
- environment and secrets are cleared unless explicitly injected through a typed boundary;
- process timeout/cancel removes descendants;
- worktree creation/cleanup remains correct after crash;
- concurrent attempts cannot write each other's workspace;
- target filesystem preserves hashes and atomicity assumptions used by CAS/SQLite procedures.

The embedding controller must construct `WorktreeManager` with durable
sessions and call `recover_incomplete(repository_mapping)` before creating a
new attempt worktree. Creation persists `PROVISIONING` before the Git side
effect; removal persists `REMOVING` before cleanup. Recovery owns both crash
windows and records `CLEANED` or observable `CLEANUP_FAILED` state.

Run the deployment-bound probes on the directory that will contain controller
state, CAS data and attempt worktrees:

```bash
uv run python scripts/dq01_preflight.py --strict --target <deployment-root>
uv run python scripts/dq01_filesystem_probe.py --root <deployment-root>
uv run pytest -q tests/security/test_sandbox.py tests/integration/test_executor.py
```

## HARD failures

Any host filesystem escape, unexpected network access, secret inheritance, cross-attempt write, or process surviving a completed mandatory cleanup check.

## Exit criteria

All containment checks pass on every supported deployment topology. If WSL and native Linux are both supported, qualify them separately; evidence from one does not qualify the other.
