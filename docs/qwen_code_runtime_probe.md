# Qwen Code runtime probe — Phase 2.4B

Local discovery found `qwen` version `0.23.3`, installed from npm as
`@qwen-code/qwen-code`. Local help exposes non-interactive prompt mode,
`--input-format stream-json`, `--output-format stream-json`, JSON FD/file
event output, `--session-id`, resume/continue, sessions management, MCP
configuration/management, ACP mode, and experimental `serve --http-bridge`.

No remote request was made. Thus wire event schema, actual tool-call shape,
same-session tool-result semantics, cancellation, reconnect, and retry remain
UNKNOWN. Candidate: **structured-message driver**, pending a bounded live probe.
`QWEN_AWESWITCH_ENTRYPOINT` remains `BLOCKED_UNVERIFIED`; it does not describe
the independent official Qwen Code CLI found here.
