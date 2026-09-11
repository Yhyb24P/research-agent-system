# Qwen Runtime Capability and Protocol Probe — Phase 2.4A

## Scope and safety boundary

This is a fact probe only. No Qwen adapter, Core change, runtime launch, remote
request, session creation, cancellation, or retry was performed. The probe
stopped before `aweswitch qw` launch because that is an unverified remote
execution path.

## Local facts

| Field | Observed fact |
|---|---|
| Launcher | `aweswitch` |
| Launcher version | `0.3.8` |
| Profile alias | `qw` |
| Profile kind in local config | `qwen` |
| Model mapping | `qwen38` → `Qwen3.8-27B Remote Workstation` |
| Standalone `qw` executable | not present on PATH |
| Authentication source | local aweswitch profile config; no credential value inspected or recorded |
| Endpoint | not configured in the redacted `qw` profile; host is therefore unknown |

`aweswitch list` and `aweswitch show qw` both resolve the profile. Its own
`--help` advertises launch support only for Claude, Codex, and OpenCode. The
installed package metadata independently says the same. Thus config presence
does not establish a Qwen transport implementation.

## Capability matrix

| Capability | State | Evidence / reason |
|---|---|---|
| Health/version | SUPPORTED (launcher only) | `aweswitch --version` returned 0.3.8 |
| Profile resolution | SUPPORTED | `aweswitch list`, `aweswitch show qw` |
| Qwen runtime executable/transport | BLOCKED | no `qw` binary; launcher documents no Qwen provider |
| Endpoint host | UNKNOWN | profile contains no endpoint field in redacted view |
| Authentication source type | SUPPORTED | local profile config, credential not inspected |
| Model identifier | SUPPORTED | local resolved mapping above |
| Session/thread/request identity | UNKNOWN | no safe real runtime transport found |
| Streaming/events | UNKNOWN | no safe real runtime transport found |
| Tool/function calling | UNKNOWN | no safe real runtime transport found |
| Same-session tool-result return | UNKNOWN | no safe real runtime transport found |
| Cancellation | UNKNOWN | no safe real runtime transport found |
| Reconnect/recovery inspection | UNKNOWN | no safe real runtime transport found |
| Error schema/retry | UNKNOWN | no safe real runtime transport found |

## Candidate selection

**Selected candidate: `BLOCKED / unsupported`.**

No live tool-capable, structured-message, or bounded-worker adapter may be
selected from a profile name or model name. A later probe needs a documented,
locally installed Qwen runtime executable or an explicitly authorized command
that establishes its transport and stable request identity without exposing
credentials or uncontrolled remote side effects.

If that evidence becomes available, the required mapping is already bounded:

| Runtime fact | Existing authoritative target |
|---|---|
| external session/request handle | `ExternalRuntimeBinding` keyed by canonical task + attempt |
| runtime attempt lifecycle | existing `team_task_runs` |
| bounded peer/context request | `messages` or `runtime_collaboration_records` |
| exact output | existing task-keyed `artifacts` |
| canonical task state | `SqliteTaskBoard` only |

The native handle must remain an external reference. It cannot replace the
TaskGraph, trusted authority, independent review, or ACC acceptance.

## Reproduction

```bash
aweswitch --version
aweswitch --help
aweswitch list
aweswitch show qw
aweswitch config path
aweswitch qw --help
command -v qw
```

Do not run `aweswitch qw` without a separately authorized live-runtime probe.
