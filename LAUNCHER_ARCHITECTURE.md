# Agent Workspace Launcher Architecture / Agent Workspace Launcher 架构冻结

Status: **LP00 architecture freeze for post-V1 productization**  
Branch: `next/agent-workspace-launcher`

This decision belongs to the post-V1 productization line. It is not part of
the immutable V1 qualification candidate and must not be used as evidence for
rc.80.

本决策属于 post-V1 产品化线，不是 V1 不可变资格候选的组成部分，也不得作为
rc.80 的资格证据。

## 1. Frozen authority boundary / 冻结的权威边界

The launcher consumes the existing Agent Collaboration Plane. It does not
create another Agent profile, registry, scheduler, policy engine, verifier,
or workflow state machine.

Launcher 只消费已有 Agent Collaboration Plane，不新建平行的 Agent Profile、
Registry、scheduler、Policy、Verifier 或工作流状态机。

```text
Human                         HUMAN actor
Cloud/Local specialist       AGENT actor
Policy/Verifier/Orchestrator SYSTEM actor

ResearchRun                  research workflow identity
RuntimeSession               one concrete Agent runtime instance
AgentRuntime                 durable adapter/runtime configuration
```

Agent skills remain descriptive. Only Policy may grant trusted capabilities.
An Agent may request work; only the controller may create a Delegation and
lease authoritative ownership.

Agent skill 仍只是描述性能力。只有 Policy 可以授予可信 capability。Agent 可以
请求工作，但只有 Controller 可以创建 Delegation 并授予权威 lease。

## 2. Process roles / 进程与命令职责

| Entry point | Responsibility | Must not do |
|---|---|---|
| `researchd` | long-running trusted control-plane daemon; owns DB sessions, recovery ordering, typed commands, event projection and runtime supervision | expose direct database mutation or delegate trusted authority to an Agent |
| `research` | daily user CLI and later TUI; sends typed commands and renders projections | persist collaboration state, mutate SQLite directly, or infer state from terminal output |
| `researchctl` | existing low-level read-only/admin/qualification surface | become the daily UX or silently start an orchestrator |

```text
research / TUI / future browser
        │ typed LocalControlCommand
        ▼
researchd
        ├─ trusted services + authoritative SQLite
        ├─ RuntimeSupervisor
        └─ monotonic read-only event projection
```

## 3. RuntimeSession contract

`RuntimeSession` represents a concrete running or attached instance. It does
not replace `AgentProfile`, `AgentRuntime`, `AgentRuntimeLease`,
`AgentInvocation`, or `ResearchRun`.

Required durable fields:

```text
runtime_session_id
runtime_id                  FK -> existing AgentRuntime
launch_mode                 PROCESS | REMOTE_HTTP | CLOUD | A2A
supervisor_state            STARTING | HEALTHY | DEGRADED | STOPPING |
                            STOPPED | LOST | RECONCILIATION_REQUIRED
external_identity           PID/start-time tuple or non-secret remote identity
started_at
last_health_at
stopped_at
exit_reason
reattach_state
version
created_at / updated_at
```

`external_identity` must be strong enough to reject PID reuse. Secrets,
provider credentials, prompts, and raw terminal streams are not durable
identity fields.

`external_identity` 必须能拒绝 PID 复用误认。secret、provider credential、prompt
和原始 terminal stream 不得成为持久化身份字段。

## 4. RuntimeSupervisor contract

The first supervisor supports only:

- `PROCESS`: an explicitly configured managed process;
- `REMOTE_HTTP`: health/attach lifecycle for a typed remote runtime;
- existing governed cloud and A2A adapter paths.

Every start/attach/stop/reconcile operation follows:

```text
validate AgentRuntime + policy
  → persist RuntimeSession intent
  → perform external side effect
  → persist observation/result
  → append AuditEvent
```

On restart, a session previously recorded as healthy is never assumed alive.
The supervisor must re-identify the external instance and either reattach,
mark it `LOST`, or mark it `RECONCILIATION_REQUIRED`.

重启后不得把原 `HEALTHY` 会话直接当作仍存活。Supervisor 必须重新识别外部
实例，然后 reattach、标记 `LOST` 或标记 `RECONCILIATION_REQUIRED`。

## 5. researchd startup barrier / 启动恢复屏障

`researchd` must not accept new mutation commands until this ordered barrier
completes:

```text
1. verify migration head; never upgrade an old backup format in place
2. verify backup state and authoritative DB/CAS integrity
3. WorkspaceDelegationService.recover_incomplete()
4. WorktreeManager.recover_incomplete(repository_mapping)
5. RuntimeSupervisor.reconcile_sessions()
6. JobManager.reconcile()
7. InvocationService recovery for non-terminal invocations
8. verify monotonic audit/event stream health
9. publish READY
```

Any unresolved unsafe state keeps the daemon non-ready and visible through a
typed health projection.

任何未解决的不安全状态都必须让 daemon 保持 non-ready，并通过类型化健康投影
显式暴露。

## 6. Command and event boundary / 命令与事件边界

All mutations use versioned typed commands containing actor attribution,
idempotency identity, target scope, and expected version where applicable.
Commands return an accepted/rejected command result; durable state changes are
observed through the monotonic event projection.

所有 mutation 都必须使用带版本的 typed command，并包含 actor 归属、幂等身份、
目标作用域和必要的 expected version。持久化变化只通过单调事件投影观测。

First command families:

```text
WorkspaceCreate
RuntimeSessionStart / RuntimeSessionAttach / RuntimeSessionStop
ResearchTaskCreate / ResearchTaskCancel
ApprovalApprove / ApprovalReject
BackupCreate / BackupVerify / RestorePlan
```

Free text may be command payload data, but an AG-UI event, terminal line, or
free-text label can never directly select a state transition.

## 7. Module boundary for LP01/LP02

Proposed source ownership:

```text
src/researchd/daemon/             composition root + startup barrier
src/researchd/runtime_sessions/   RuntimeSession domain/storage service
src/researchd/supervisor/         process/remote adapters and reconciliation
src/research/                     daily CLI/TUI client; no trusted persistence
```

Existing modules remain authoritative for registry, collaboration, policy,
verification, workspace, jobs, backup and event projection. New modules call
those services instead of wrapping them in parallel domains.

## 8. Explicit non-goals for the first milestone

```text
Docker or Kubernetes supervisor
PTY/terminal parsing as protocol
SSHFS workspace transport
browser UI
public multi-tenant service
automatic provider fallback
Agent self-registration with trusted capabilities
Agent self-verification
direct CLI-to-SQLite mutations
```

## 9. LP00 exit criteria

LP00 is frozen when review confirms:

- the three command roles do not overlap;
- `RuntimeSession` has no semantic collision with `ResearchRun` or
  `AgentRuntime`;
- the startup barrier covers every existing recovery service;
- every mutation crosses a typed command boundary;
- no duplicate Agent Profile/Registry/Policy/Verifier domain is introduced;
- implementation can begin with LP01 and LP02 without changing these
  authority boundaries.

LP00 只是架构冻结，不是实现完成声明。

## 10. Implementation status / 实施状态

The productization branch now contains the first LP01/LP02 foundation:

- migration `0020` persists `RuntimeSession` and typed START/ATTACH/STOP
  command receipts;
- PROCESS supervision uses PID, process start ticks, and host boot identity to
  reject PID reuse; REMOTE_HTTP accepts only HTTPS or loopback HTTP endpoints
  registered on the existing `AgentRuntime`;
- intent is committed before each external side effect, while observation and
  command result are committed afterward in the same monotonic audit stream;
- restart reconciliation never relaunches an uncertain persisted intent;
- `ResearchDaemon` remains non-ready until all eight frozen recovery phases
  pass.
- the concrete `researchd init` / `researchd serve` lifecycle composes the
  trusted services once, exposes loopback health and typed RuntimeSession
  commands, and is covered by an independent-process startup test.
- a single strict JSON configuration supplies absolute authoritative paths,
  named Git repositories and fixed typed-job argv arrays; unknown fields,
  shell strings and non-loopback binds are rejected.
- an independent restart test proves live PROCESS reattachment and monotonic
  audit offsets across daemon replacement before issuing a typed STOP.
- independent crash-window processes prove that a persisted START intent is
  never relaunched and a persisted STOP intent is completed by reconciliation.
- `researchd validate` touches no state, while `researchd inspect` exposes a
  non-secret projection and canonical configuration digest without command
  arguments.

产品化分支现已实现首批 LP01/LP02 基础：持久化运行实例和类型化命令凭证、拒绝
PID 复用的 PROCESS supervisor、受限 REMOTE_HTTP attach、先 intent 后副作用的审计
顺序，以及八阶段全部通过后才 READY 的 daemon gate；具体 `researchd init` / `serve`
生命周期现已完成，并由独立进程启动测试覆盖。
单一严格 JSON 配置负责绝对路径、具名 Git repository 和固定 job argv；未知字段、
shell 字符串及非 loopback 监听均被拒绝。独立重启测试还证明了存活 PROCESS 的重新
附着和 daemon 更替前后的审计 offset 连续性。
独立 crash-window 进程进一步证明：已持久化但结果未知的 START 不会被重放，STOP 则由
重启协调完成。`researchd validate/inspect` 可在不启动 daemon 的情况下验证配置；
inspect 不回显固定命令参数。

This is still an implementation milestone, not completion of the productized
launcher. The daily `research` client and LP03 managed Agent pilot remain open.

这仍是实现里程碑，不代表产品化 Launcher 已完成。日常 `research` client 和 LP03
managed Agent pilot 仍未完成。
