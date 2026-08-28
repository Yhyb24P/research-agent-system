# DQ01 — Host, Sandbox, and Filesystem Qualification

DQ01 proves the boundary on a named target host. The existence of
`BubblewrapBackend` and green unit tests is not itself a qualification result.
The result is valid only for the host/runtime manifest captured by DQ00.

## Non-destructive preflight

Run the following before destructive or race-oriented tests:

```text
.venv/bin/python scripts/dq01_preflight.py --strict --output dq01-preflight-evidence.json
```

The report records the OS/kernel/architecture, Bubblewrap path/version/mode/file
capabilities, user-namespace limit, and cgroup filesystem. It fails closed for
missing Bubblewrap, setuid or unexpected file capabilities, disabled user
namespaces, or missing cgroup v2. The report contains no environment values or
credentials.

## Execution-surface audit (current RC)

The `subprocess` call sites were reviewed against the trusted-controller
boundary:

| Surface | Purpose | Boundary decision |
|---|---|---|
| `executor/sandbox.py` | Starts Bubblewrap and collects bounded output | Allowed only for typed `CommandSpec`; network is restricted to `none` |
| `executor/jobs.py` | Starts the detached durable runner | Trusted backend wrapper; runner itself starts the Bubblewrap argv built from typed `JobSpec` |
| `executor/job_runner.py` | Forwards signals and persists runner status | Trusted backend wrapper; no agent-provided command construction |
| `executor/worktree.py` | Runs fixed `/usr/bin/git -C` operations | Trusted typed Git operation; arguments are not shell strings |

No direct model, plugin, debug, or fallback path was found that can invoke a
host command outside these typed controller/backend surfaces.

The current RC also includes a broker-level regression that repeatedly replaces
a workspace parent with a symlink while writes are attempted. The test accepts
only a denied operation or a write anchored inside the workspace and asserts
that the outside directory is unchanged. This is evidence for the controller's
directory-FD defense, not a substitute for a target-filesystem stress run.

It also starts a host-side process with a unique environment marker and checks
that the marker cannot be read through either the host PID's `/proc` path or
`/proc/self/environ` from inside Bubblewrap. This demonstrates the current
Linux/WSL PID and environment boundary; it remains scoped to the tested host.

## Qualification cases still pending

These require the target deployment and must not be marked green from static
inspection alone:

- host home and `/proc` visibility, including a host PID namespace check;
- no-network behavior and secret-environment absence;
- static and concurrent symlink/TOCTOU escape attempts;
- artifact traversal and mount-target validation;
- cancellation with no orphan descendants;
- CPU, memory, file-size, wall-time, and output quotas;
- scheduler/container/GPU-specific bypass paths.

The current WSL2 preflight is an environment observation, not a production
qualification certificate. A failure in the race tests should trigger the
smallest implementation fix, a new RC, and rerun of affected DQ evidence.
