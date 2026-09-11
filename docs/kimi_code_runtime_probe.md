# Kimi Code runtime probe — Phase 2.4B

Local discovery found native `kimi` version `0.39.1`. Local help exposes
non-interactive prompt mode, `--output-format stream-json`, `--session`,
`--continue`, `doctor`, provider management, and `acp` as an Agent Client
Protocol server over stdio.

No remote request was made. Tool/MCP details, event schema, tool-result return,
cancellation, reconnect, and retry remain UNKNOWN. Candidate:
**structured-message driver**, pending a bounded live ACP or CLI probe.

## Phase 2.5A bounded ACP result

An isolated `kimi acp` stdio JSON-RPC probe sent only `initialize` and
`initialized`, then exited 0. Real stdout confirmed protocol version 1,
agent identity/version, session load/list/resume/close/delete/fork capability,
prompt image/embedded-context capability, MCP HTTP/SSE capability, and logout.
The response offered a terminal login auth method, but no verified active auth
state. The probe therefore stopped at `BLOCKED_AUTH_REQUIRED`; no task was sent.

| Capability | State | Evidence |
|---|---|---|
| initialize / initialized | SUPPORTED | real ACP stdout |
| external runtime identity | SUPPORTED | real agent identity/version |
| session inspect/resume capability | SUPPORTED | ACP session capabilities |
| MCP transport capability | SUPPORTED | ACP MCP HTTP/SSE capabilities |
| client task / lifecycle events | BLOCKED_AUTH_REQUIRED | no authenticated task permitted |
| runtime tool request / same-ID response | BLOCKED_AUTH_REQUIRED | no authenticated task permitted |
| cancel / interrupt | BLOCKED_AUTH_REQUIRED | no authenticated task permitted |
| restart reconcile / retry schema | BLOCKED_AUTH_REQUIRED | no authenticated session permitted |
