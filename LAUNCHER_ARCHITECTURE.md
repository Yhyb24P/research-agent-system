# Agent Workspace Launcher Architecture / Agent Workspace Launcher 架构冻结

Status: **LP00 architecture freeze for post-V1 productization**  
Branch: `next/agent-workspace-launcher`

**Current status (2026-08-31):** the productization mainline described by this
architecture completed through PX09 at `40d83ec`, with exact-head CI
`33351341328` green. The dated LP01/LP02 and LP03 statements below are retained
as architecture/audit history, not as the current implementation status.

**当前状态（2026-08-31）：** 本架构所描述的产品化主线已在 `40d83ec` 完成至
PX09，精确 head CI `33351341328` 全绿。下文按日期记录的 LP01/LP02/LP03 表述
保留为架构与审计历史，不再表示当前实现状态。

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
- migration `0021` persists a generic daemon command receipt before dispatch,
  replays terminal results without repeating side effects, and leaves an
  interrupted `ACCEPTED` receipt fail-closed for operator reconciliation;
- migration `0022` persists a one-to-one trusted RuntimeLaunchProfile for each
  managed AgentRuntime. Public runtime requests cannot carry launch details;
  the daemon verifies the stored profile digest and snapshots the resolved
  specification plus profile hash on the RuntimeSession before side effects;
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
- a versioned `DaemonCommand`/`DaemonCommandResult` contract
  (`command_version=1`, command identity, accepted/rejected envelope) bounds
  every daemon-dispatched mutation, and the run cancel, work-order approve and
  human-decision HTTP routes now cross the same `ResearchDaemon.execute()`
  readiness gate as RuntimeSession commands, returning `202` with the typed
  envelope. The composition root now injects the real `ResearchOrchestrator`
  built from existing authorities only — `CollaborationGateway`
  (delegations/invocations/selector), `RecordingPolicyEngine` over
  `DeterministicPolicyEngine`, `ApprovalService` and `JobManager`, with empty
  capabilities — so gate-dispatched control mutations execute against the
  trusted controller. The verification driver slot is filled by the concrete
  `LocalVerificationDriver` (PX03-01); the cloud/executor Agent adapters
  remain unwired until managed Agents attach (LP03/LP04).
- PX00 external request DTOs now exclude trusted actor fields. The HTTP adapter
  derives a HUMAN actor before constructing internal commands, and rejects
  caller-supplied `SYSTEM`/actor attribution before dispatch. `researchd init`
  now creates an owner-only 256-bit local credential and every non-health HTTP
  read, stream and mutation authenticates it before routing. The credential is
  excluded from database, audit, backup, inspect and Agent context surfaces.
  Migration `0022` also closes the public launch-spec surface with a
  server-owned RuntimeLaunchProfile and persisted resolved-spec/hash snapshot.
- PX00 operator reconciliation: an interrupted `ACCEPTED` generic receipt no
  longer wedges the daemon. A narrow, authenticated recovery route
  `POST /api/daemon-commands/{command_id}/resolve` stays reachable while the
  daemon is FAILED (it bypasses the readiness gate but keeps Bearer-token
  authentication and persists the operator command identity). A
  command-specific observer first observes the family's authoritative state;
  the operator may only abandon an undetermined outcome
  (`OPERATOR_ABANDONED`) — there is no free-form terminal override. The
  target receipt, the resolution receipt and the audit events commit in one
  transaction, so a crash can never leave a resolution half-applied, and a
  terminal target can only be replayed, never re-resolved to a different
  result. `researchctl daemon-command list/resolve` offers the same channel
  offline against the SQLite database.
- PX00-05 typed command families: `WorkspaceCreate`, `ResearchTaskCreate`,
  `WorkOrderReject`, `CollaborationMessageSend`, `BackupCreate`,
  `BackupVerify` and `RestorePlan` now cross the same `ResearchDaemon.execute()`
  gate. The control authority gains `create_workspace`, `create_research_task`,
  `reject` and `send_collaboration_message` (fail closed without an
  orchestrator); the backup authority (`BackupCommandService`) is bound to the
  daemon's own database and artifact root and also fails closed when absent.
  Reject converges a `WAITING_APPROVAL` order and its pending approval to
  `FAILED`/`REJECTED` (the run fails with `APPROVAL_REJECTED`), symmetric with
  a policy denial. Verify and plan are pure reads that never copy or write, so
  an interrupted receipt for either can only be abandoned
  (`OPERATOR_ABANDONED`). Reconciliation gained one observer per new family,
  including snapshot-tree observation for backup create and read-only
  observation for verify/plan.
- PX00-06 event projection completion: run event payloads now carry
  `actor_type`/`actor_id` from the authoritative audit stream; a new
  `/api/system-stream` SSE route streams the global (run-less) audit events
  with `Last-Event-ID`/`after` resume and optional follow, mirroring the JSON
  `/api/system-events` projection; `GET /api/collaboration-messages/{id}`
  reads a stored message record including its classification. The AG-UI
  projection keeps its explicit key selection, so `LOCAL_ONLY`/`SECRET`
  bodies remain redacted from the AG-UI stream.
- PX01-03 ManagedAgentStart: the public launch surface is now
  `POST /api/agents/{agent_id}/start` with an optional `runtime_id` — the
  daemon resolves the launch spec from the trusted launch catalog
  (`ManagedAgentStartService` over the existing registry and
  `RuntimeLaunchProfileService`) and dispatches the internal
  `RuntimeSessionStart`/`RuntimeSessionAttach` command through the
  supervisor. The runtime session identity is derived from the command
  identity, so a replayed command maps to the same session. The arbitrary
  public `/api/runtime-sessions/start` and `/api/runtime-sessions/attach`
  routes are disabled; the per-session stop route remains.
- PX01-04 Agent aliases: `AgentAliasService` resolves an operator-typed
  alias through the trusted `AgentProfile.labels["cli_alias"]` label.
  Case normalization is documented and enforced: both the stored label
  and the queried alias are stripped and lower-cased before comparison,
  so alias matching is case- and surrounding-whitespace-insensitive.
  Only enabled profiles participate; when two or more enabled profiles
  claim the same normalized alias, resolution is rejected as ambiguous
  (`AgentAliasAmbiguous`), and an unclaimed alias raises
  `AgentAliasNotFound`.
- PX02-02 Client transport: `researchd.client.transport.ResearchClient`
  is the only channel through which the daily `research` client reaches
  the controller; it never opens the SQLite database. Reads and SSE
  streams authenticate with the owner-only control credential, and
  mutations POST a typed external request carrying a generated or
  caller-supplied command identity (`cmd_<32 hex>`), so a retry replays
  instead of duplicating. The transport separates a durable `REJECTED`
  envelope from a 409 that only means the daemon is not ready or the
  command identity was reused with a different request, and resumes SSE
  streams from the last observed `id:` offset.
- PX02-03 research lifecycle: `research init` delegates bootstrap to the
  trusted `researchd init` executable (the client itself never initializes
  or migrates the database), and `research status` prints one JSON document
  with reachability and readiness. Without a subcommand, `research` probes
  the daemon and, when none is reachable, spawns `researchd serve` as a
  controller process whose log lands in `<state_root>/daemon.log`; it remains
  independent when the interactive shell exits. The shell is entered only
  after the health probe reports READY — a FAILED or still-starting daemon
  is surfaced with its failed startup phase, never bypassed.
- PX02-04 Line shell: the interactive entry now runs a line-oriented shell
  with the first command batch — `status`, `agent list` / `agent use` /
  `agent remove` (a session-local working set; the client never mutates
  registered agents), `run list`, `task create`, `task cancel`, `msg`,
  `events watch` (SSE follow, Ctrl-C stops), `approve`, `reject` and
  `quit`. Lines parse through a strict parser (quoted arguments,
  `--key value` options, per-command arity); every command crosses the
  authenticated transport, and parse or transport errors are reported
  without killing the shell.
- PX03-01 VerificationDriver: `LocalVerificationDriver` fills the
  orchestrator's verification slot. It extracts the acceptance criteria
  from the WorkOrder contract (an empty acceptance list is refused — zero
  criteria would auto-pass with no evidence), maps the attempt's
  trusted `metrics`/`reproducibility` CAS artifacts onto the criteria,
  refuses results that reference unknown artifacts, and delegates the
  judgment to the `VerifierEngine`. The executor cannot self-verify: the
  outcome derives from hash-verified immutable artifacts and trusted
  execution-step records; executor-reported claims are recorded as claims
  and never become verifier inputs. `compose_daemon` now wires the
  driver, so verification no longer fails closed for lack of a verifier.

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
带版本的 `DaemonCommand`/`DaemonCommandResult` 合同（`command_version=1`、命令身份、
accepted/rejected envelope）现已约束所有经 daemon 派发的 mutation；run cancel、
work-order approve 和 human-decision 三个 HTTP 路由与 RuntimeSession 命令一样强制经过
`ResearchDaemon.execute()` readiness gate，并以 `202` 返回类型化 envelope。组合根现已
注入真实 `ResearchOrchestrator`——仅由现有权威构成：`CollaborationGateway`
（delegations/invocations/selector）、基于 `DeterministicPolicyEngine` 的
`RecordingPolicyEngine`、`ApprovalService` 与 `JobManager`，capabilities 默认为空——
因此经 gate 派发的控制面 mutation 均由可信 controller 真实执行。验证 driver 槽位
已由具体的 `LocalVerificationDriver` 填充（PX03-01）；cloud/executor Agent
adapter 仍待受管 Agent 接入（LP03/LP04）。
PX00 外部请求 DTO 现已排除可信 actor 字段；HTTP adapter 在构造内部命令前由服务端绑定
HUMAN actor，调用方自报的 `SYSTEM` 或 actor attribution 会在派发前被拒绝。
`researchd init` 现会生成 owner-only 256-bit 本地凭据；除 health 外的 HTTP 读取、stream
和 mutation 都在路由前认证。凭据不会进入数据库、审计、备份、inspect 或 Agent context。
Migration `0022` 还以 server-owned RuntimeLaunchProfile 和持久化的解析规格/hash snapshot
关闭了公开 launch-spec 输入面。
PX00 operator 回执协调：中断遗留的 `ACCEPTED` 通用回执不再永久卡死 daemon。一条窄的、
带认证的恢复路由 `POST /api/daemon-commands/{command_id}/resolve` 在 daemon FAILED 时
仍然可达（绕过 readiness gate，但保留 Bearer token 认证，并持久化 operator 命令身份）。
命令族专属 observer 先观察权威状态；operator 只能放弃无法判定的结果
（`OPERATOR_ABANDONED`），不存在自由改写终态的入口。目标回执、resolution 回执与审计
事件在同一事务提交，crash 不可能留下半应用的 resolution；已终态的目标只能重放，
不能被再次解析出不同结果。`researchctl daemon-command list/resolve` 提供同一通道的
离线 SQLite 入口。
PX00-05 类型化命令族：`WorkspaceCreate`、`ResearchTaskCreate`、`WorkOrderReject`、
`CollaborationMessageSend`、`BackupCreate`、`BackupVerify` 与 `RestorePlan` 现在同样
强制经过 `ResearchDaemon.execute()` gate。控制权威新增 `create_workspace`、
`create_research_task`、`reject` 与 `send_collaboration_message`（缺少 orchestrator
时失败关闭）；备份权威（`BackupCommandService`）绑定 daemon 自身的数据库与 artifact
根目录，缺失时同样失败关闭。Reject 把 `WAITING_APPROVAL` 工单及其 pending 审批收敛为
`FAILED`/`REJECTED`（run 以 `APPROVAL_REJECTED` 失败），与策略拒绝对称。verify 与 plan
是纯读操作，从不复制或写入，其中断回执只能被放弃（`OPERATOR_ABANDONED`）。
reconciliation 为每个新命令族增加了专属 observer，包括 backup create 的快照树观察与
verify/plan 的只读观察。
PX00-06 事件投影补全：run event payload 现携带权威审计流的 `actor_type`/`actor_id`；
新增 `/api/system-stream` SSE 路由，流式输出全局（无 run）审计事件，支持
`Last-Event-ID`/`after` 续读与可选 follow，与 JSON `/api/system-events` 投影同源；
`GET /api/collaboration-messages/{id}` 按 id 读取消息记录（含 classification）。
AG-UI 投影保持显式选键，`LOCAL_ONLY`/`SECRET` 正文在 AG-UI 流中依旧脱敏。
PX01-03 ManagedAgentStart：公开启动面收敛为 `POST /api/agents/{agent_id}/start`
（仅可选 `runtime_id`）——daemon 经 `ManagedAgentStartService`（复用既有
registry 与 `RuntimeLaunchProfileService`）从受信 launch catalog 解析 launch
spec，再向 supervisor 派发内部 `RuntimeSessionStart`/`RuntimeSessionAttach`
命令；runtime session 身份由命令身份派生，同一命令重放映射到同一会话。
原先任意的公开 `/api/runtime-sessions/start` 与 `/api/runtime-sessions/attach`
路由已禁用，按会话的 stop 路由保留。
PX01-04 Agent aliases：`AgentAliasService` 经受信
`AgentProfile.labels["cli_alias"]` 标签解析 operator 输入的别名。大小写
归一化已文档化并强制执行：存储标签与查询别名均先去除首尾空白并转小写
再比较，即别名匹配对大小写与首尾空白不敏感。仅启用中的 profile 参与
解析；两个及以上启用 profile 声称同一归一化别名时，解析以歧义拒绝
（`AgentAliasAmbiguous`）；无人声称的别名抛出 `AgentAliasNotFound`。
PX02-02 客户端传输层：`researchd.client.transport.ResearchClient` 是日常
`research` client 到达 controller 的唯一通道；它从不打开 SQLite 数据库。
读取与 SSE 流使用 owner-only 控制凭据认证；mutation 以生成或调用方提供的
命令身份（`cmd_<32位hex>`）POST 类型化外部请求，因此重试走重放而非重复
执行。传输层将持久的 `REJECTED` envelope 与仅表示 daemon 未就绪或命令身份
被不同请求复用的 409 区分开，并能从最后观察到的 `id:` offset 续读 SSE 流。
PX02-03 research lifecycle：`research init` 将 bootstrap 委托给受信
`researchd init` 可执行文件（client 自身从不初始化或迁移数据库）；
`research status` 输出一份含可达性与就绪状态的 JSON 文档。不带子命令时，
`research` 探测 daemon；无 daemon 可达则以子进程 spawn `researchd serve`
（日志落 `<state_root>/daemon.log`）；它独立于 client 窗口，退出 shell 不会停止它。
只有健康探测报告 READY 后才进入 shell——FAILED 或仍在启动中的 daemon
会连同其失败阶段一起呈现，绝不被绕过。
PX02-04 行式 shell：交互入口现为行式 shell，首批命令——`status`、
`agent list`/`agent use`/`agent remove`（会话本地工作集；client 从不
变更已注册 agent）、`run list`、`task create`、`task cancel`、`msg`、
`events watch`（SSE follow，Ctrl-C 停止）、`approve`、`reject` 与
`quit`。行经严格 parser 解析（引号参数、`--key value` 选项、逐命令
参数个数校验）；每条命令都走认证 transport，解析或传输错误只报告、
不终止 shell。
PX03-01 VerificationDriver：`LocalVerificationDriver` 填上 orchestrator
的验证槽位。它从 WorkOrder 合同提取验收标准（空验收列表被拒绝——零
标准会无证据自动通过），把 attempt 名下受信的 `metrics`/
`reproducibility` CAS 产物映射到对应标准，拒绝引用未知产物的执行
结果，判断本身交给 `VerifierEngine`。executor 不得自我验证：结果
派生自经 hash 校验的不可变产物与受信执行步骤记录；executor 上报的
claims 只作为 claims 记录，永不成 verifier 输入。`compose_daemon` 已
接线该 driver，验证不再因缺少 verifier 而失败关闭。

PX03-02 managed executor routing：产品 composition 已将 PROCESS adapter
注册到 `AgentAdapterCatalog`，不固定任何 Agent 身份。产品 selector 要求
启用的已安装 runtime、有效 lease，以及 LaunchProfile hash 仍与可信目录
一致的 HEALTHY RuntimeSession。canonical invocation 只调用 Registry 持有的
loopback endpoint，绝不再次执行启动 argv。

PX03-02 managed executor routing registers the PROCESS adapter in
`AgentAdapterCatalog` without fixing any Agent identity. Product selection
requires an enabled installed runtime, a live lease, and a HEALTHY
RuntimeSession whose LaunchProfile hash still matches the trusted catalog.
Canonical invocation targets only the registry-owned loopback endpoint and
never executes the launch argv a second time.

At the 2026-08-30 architecture snapshot, this was still an implementation
milestone rather than completion of the productized launcher; the daily
`research` client and LP03 managed Agent pilot were then open. See the current
status note at the top of this document for the superseding final state.

在 2026-08-30 的架构快照中，这仍是实现里程碑，不代表产品化 Launcher 已完成；当时
日常 `research` client 和 LP03 managed Agent pilot 仍未完成。最新终态请以本文顶部
的当前状态说明为准。

## 11. LP01/LP02 completion audit / 完成度审计

Audit date: **2026-08-30**. A green test suite is not used as a substitute for
the frozen requirements. The current evidence is:

| Requirement | Status | Authoritative evidence |
|---|---|---|
| exact schema check, DB/CAS sanity and eight ordered recovery phases | PASS | `daemon/startup.py`, migration `0022`, daemon integration tests |
| non-ready mutation rejection and visible loopback health | PASS | `ResearchDaemon.execute`, `/api/health`, failed-start independent-process test |
| unresolved runtime/workspace/worktree/job/invocation recovery blocks READY | PASS | fail-closed post-recovery checks in `daemon/startup.py` |
| durable RuntimeSession and idempotent START/ATTACH/STOP receipts | PASS | migration `0020`, `runtime_sessions/service.py`, concurrency/replay tests |
| PROCESS identity, PID-reuse rejection, restart reattach and crash windows | PASS | supervisor driver and independent-process restart/crash tests |
| constrained REMOTE_HTTP attach through existing AgentRuntime | PASS | `RemoteHttpDriver`, registry endpoint checks and integration tests |
| `researchd` owns existing orchestrator/policy/approval command paths | PASS | `compose_daemon` builds the real `ResearchOrchestrator` from `CollaborationGateway`, `RecordingPolicyEngine(DeterministicPolicyEngine())`, `ApprovalService` and `JobManager` with empty capabilities; gate-dispatched cancel/approve/human-decision mutations execute against it (`daemon/composition.py`, composed-daemon mutation tests in `tests/integration/test_daemon.py`); the verification driver slot is filled by `LocalVerificationDriver` (PX03-01, `tests/integration/test_verification_driver.py`), while the cloud/executor adapters remain a deliberate gap |
| every existing HTTP mutation crosses `ResearchDaemon.execute()` | PASS | run cancel, work-order approve and human-decision routes dispatch typed commands through the daemon readiness gate (`api/web.py`, gate tests) |
| versioned accepted/rejected result envelope for every command family | PASS | `daemon/contracts.py`, `DaemonCommandDispatcher` and envelope assertions in daemon web/AG-UI tests |
| durable generic command receipt with idempotency persistence | PASS | migration `0021`, `DurableDaemonCommandService`, `/api/daemon-commands`, replay/conflict tests and independent-process crash-window test |
| explicit operator resolution command for interrupted generic receipts | PASS | `daemon/reconciliation.py` (command-family observers, atomic convergence, guarded UPDATE), non-ready-reachable authenticated `POST /api/daemon-commands/{id}/resolve` in `api/web.py`, `researchctl daemon-command list/resolve`, observer/idempotency/conflict tests in `tests/integration/test_daemon_resolution.py` and the failed-daemon recovery test in `tests/integration/test_daemon_process.py` |
| Workspace, ResearchTask, Approval and Backup typed command families | PASS | PX00-05: `WorkspaceCreate`/`ResearchTaskCreate`/`WorkOrderReject`/`CollaborationMessageSend`/`BackupCreate`/`BackupVerify`/`RestorePlan` commands in `daemon/contracts.py`; dispatcher branches plus extended `ControlMutationAuthority` and `BackupMutationAuthority` in `daemon/dispatcher.py`; `LocalControlAPI` methods in `api/control.py`; seven new routes in `api/web.py`; `BackupCommandService` in `backup/commands.py`; one observer per family in `daemon/reconciliation.py`; tests in `tests/integration/test_daemon_command_families.py`, `test_daemon_web.py`, `test_daemon_resolution.py` and `test_daemon_commands.py` |

Therefore LP02's durable runtime/supervision slice is implemented, while LP01
as a complete trusted mutation host is **not frozen complete**. The shared
command/result contract, the migration of the existing HTTP mutations through
the readiness gate, the real Orchestrator/policy/approval injection into the
composition root, and the Workspace, ResearchTask, Approval and Backup typed
command families are done; the `VerificationDriver` wiring is filled by
`LocalVerificationDriver` (PX03-01), and the remaining gap is the
cloud/executor Agent adapters (LP03/LP04). LP03 must not paper over these
LP01 gaps with a pilot-only bypass. The next implementation order is: the
managed Agent pilot.

审计结论：LP02 的持久化运行实例与 supervision 切片已经实现；LP01 作为完整可信
mutation host 尚未冻结完成。统一命令合同、现有 HTTP mutation 经 readiness gate 的迁移、
组合根中真实 Orchestrator/Policy/Approval 的注入，以及 Workspace、ResearchTask、
Approval、Backup 类型化命令族均已完成；`VerificationDriver` 接线已由
`LocalVerificationDriver`（PX03-01）填充，剩余缺口为 cloud/executor Agent
adapter（LP03/LP04）。
LP03 不得用 pilot 专用旁路掩盖这些缺口；下一步顺序为：进入受管 Agent pilot。
