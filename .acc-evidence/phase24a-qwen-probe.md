# Sanitized Phase 2.4A evidence

- `aweswitch --version`: exit 0; observed `0.3.8`.
- `aweswitch list`: exit 0; observed alias `qw`, kind `qwen`, model key `qwen38`.
- `aweswitch show qw`: exit 0; observed display model `Qwen3.8-27B Remote Workstation`; no endpoint or credential value recorded.
- `aweswitch --help`: exit 0; observed supported providers listed as Claude, Codex, OpenCode.
- `aweswitch qw --help`: exit 0; observed only wrapper options, not a Qwen wire protocol.
- `command -v qw`: exit 1; no standalone executable on PATH.
- Installed launcher metadata: supported providers are Claude, Codex, OpenCode.

No live Qwen launch, remote request, session creation, tool call, cancellation,
or reconnect test was attempted. Result: `BLOCKED / unsupported`.
