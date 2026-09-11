# Sanitized Kimi ACP live probe evidence

- Command: `kimi acp` in an isolated temporary working directory; stdin carried
  only ACP `initialize` then `initialized`; timeout 12 seconds.
- Exit: 0.
- Observed stdout summary: JSON-RPC initialize response, protocol version 1,
  agent identity `Kimi Code CLI` version 0.39.1.
- Observed capability categories: load-session, session list/resume/close/delete/fork,
  prompt image and embedded-context, MCP HTTP/SSE, and logout.
- Observed auth: terminal login method only. No credential was read or recorded.
- No task/prompt, filesystem write, shell, network tool, git, MCP server, cancel,
  session mutation, artifact, acceptance, or admin action was requested.
- Stop result: `BLOCKED_AUTH_REQUIRED` for task/lifecycle/tool/cancel/recovery probes.
