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

## HARD failures

Any host filesystem escape, unexpected network access, secret inheritance, cross-attempt write, or process surviving a completed mandatory cleanup check.

## Exit criteria

All containment checks pass on every supported deployment topology. If WSL and native Linux are both supported, qualify them separately; evidence from one does not qualify the other.
