# Known Limitations

The V1 architecture and tests deliberately leave these deployment-dependent limitations visible:

- SQLite is single-host/single-controller oriented; PostgreSQL and distributed queues are deferred.
- The local durable Job backend offers operation-ID deduplication and reconciliation, not universal exactly-once execution.
- GPU is optional for the current CPU/local-model workflow. When enabled, GPU isolation/resource enforcement is not promised by the local backend. V1 provides durable logical admission leases, but hardware visibility and enforcement require a separately validated scheduler/container deployment.
- Bubblewrap and filesystem checks are validated on the current Linux environment; mount races, WSL differences, and other kernels need target-environment testing.
- Provider structured-output behavior, transient retry/backoff, and provider-side retention/account configuration require deployment-specific review.
- Backup snapshots are checksum-verified but encryption, off-host retention, restore drills, and key management are operational responsibilities.
- The local control API is an in-process/CLI façade; no public HTTP service is included.
- A2A/MCP adapters use dependency-free tested subsets and require external protocol conformance tests for a chosen production SDK/server.
- Automatic retry policy, multi-tenant authorization, Git push/deployment, and autonomous policy changes are intentionally absent.
