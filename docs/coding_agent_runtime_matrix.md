# Coding-agent runtime matrix — Phase 2.4B discovery

All entries are local executable facts only; no runtime was launched for a model turn.

| Runtime | Identity/version | Transport candidate | Session identity | Tools/MCP | Streaming/events | Same-session result | Cancel/recovery | Candidate |
|---|---|---|---|---|---|---|---|---|
| Codex app-server | `codex-cli 0.154.0` | stdio JSON-RPC | SUPPORTED | SUPPORTED | SUPPORTED | SUPPORTED | SUPPORTED | live tool-capable driver (already evidenced) |
| Qwen Code | `qwen` 0.23.3, npm package `@qwen-code/qwen-code` | CLI stream-json / ACP / experimental HTTP daemon | SUPPORTED (`--session-id`, resume, continue, sessions) | SUPPORTED (MCP commands/config) | SUPPORTED (`--output-format stream-json`, JSON FD/file) | SUPPORTED candidate (`--input-file` bidirectional sync); live semantics unverified | UNKNOWN | structured-message driver candidate |
| Kimi Code | `kimi` 0.39.1, local native CLI | ACP server over stdio | SUPPORTED (`--session`, `--continue`) | UNKNOWN | SUPPORTED (`--output-format stream-json`) | UNKNOWN | UNKNOWN | structured-message driver candidate |

Evidence: `.acc-evidence/phase24b-qwen-code-probe.md` and
`.acc-evidence/phase24b-kimi-code-probe.md`. “SUPPORTED” means exposed by the
local CLI help, not a completed remote runtime turn.
