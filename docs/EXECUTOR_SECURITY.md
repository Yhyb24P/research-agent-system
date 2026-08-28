# Local Executor Security Boundary

## Enforced architecture

The Local Executor is untrusted. It receives a `GrantedWorkOrder` and can act only through `CapabilityBroker`. The broker has no host-shell method: generic `sandbox.shell` accepts an argv vector and is executed only by `BubblewrapBackend`. Trusted Git worktree operations are fixed-argv controller operations, not agent shell strings.

Each Attempt receives a detached Git worktree at a unique Attempt-keyed path. The base commit, repository ID, local path, environment digest, sandbox backend, and creation time are persisted in `attempt_worktrees`. Existing or dirty Attempt paths are never silently reused.

## BubblewrapBackend guarantees

The selected concrete backend requires Linux bubblewrap and user namespaces. It creates:

- new user and PID namespaces;
- a new network namespace with no routable interface (`network=none` only);
- a minimal filesystem containing read-only `/usr`, runtime libraries, synthetic `/proc` and `/dev`, bounded tmpfs `/tmp`, and the one writable Attempt worktree;
- no host `/etc`, `/home`, Docker socket, credentials, or unrelated filesystem;
- a cleared environment rebuilt from a small controller allowlist;
- a new process/session boundary with process-group termination and bubblewrap parent-death behavior.

`prlimit` enforces CPU seconds, address-space memory, and per-file size. The controller enforces walltime and total captured-output bytes and terminates the full sandbox when either is exceeded. Explicit cancellation uses the same process-group cleanup path.

Security tests execute real bubblewrap processes and prove traversal/symlink reads cannot see host `/etc`, curl has no route, host secret environment variables disappear, timed-out descendants cannot write later, output is capped, cancellation completes, and memory/file-size limits take effect.

## Capability Broker

Requests use a closed capability enum and typed parameters. Workspace paths must be relative, traversal-free, resolve inside the Attempt root, and final writes use `O_NOFOLLOW`. Test execution invokes the mounted local runtime as an argv vector. Large bounded output is stored as a `LOCAL_ONLY` artifact and only compact head/tail text is returned inline.

Every side-effecting request ID is reserved in `execution_steps` before execution. A completed duplicate returns the persisted result; changed parameters/capability/Attempt under the same ID are rejected. An operation left `IN_PROGRESS` after a crash is not blindly repeated.

Attempt dispatch has the same persisted reuse rule through `executor_dispatches`. Local model failure produces explicit `model_unavailable`; the worker has no cloud-model dependency or fallback callback.

## Explicitly unsupported/not claimed

- Non-Linux hosts and systems without usable bubblewrap/user namespaces.
- `restricted` or `full` sandbox networking; this backend accepts only `none`.
- GPU isolation; GPU Job requests fail until a tested GPU-aware backend exists.
- Aggregate worktree disk quota and file-count quota. Per-file limits are enforced, but a filesystem/project quota backend is required before promising aggregate disk enforcement.
- Adversarial symlink/mount race resistance beyond resolution plus `O_NOFOLLOW`; this remains a TASK 08 hardening Gate.
- Arbitrary writable external mounts. Runtime mounts are read-only unless trusted controller configuration explicitly says otherwise.

Startup/configuration must fail rather than silently replace this backend with an unsandboxed subprocess implementation.
