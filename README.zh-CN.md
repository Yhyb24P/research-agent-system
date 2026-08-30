# Research Agent System（中文版）

[![control-plane-quality](https://github.com/Yhyb24P/research-agent-system/actions/workflows/quality.yml/badge.svg)](https://github.com/Yhyb24P/research-agent-system/actions/workflows/quality.yml)

[English](README.md)

Research Agent System 是面向持久化、受策略约束研究流程的 **Agent 协作面 +
可信控制面**。系统接入的实体是 Agent；框架、provider、协议和运行位置都是
`AgentRuntime` 的实现细节。

Agent 可以提出计划、执行工作单、审查结果和完成 specialist 分析，但不能拥有工作流
状态、给自己授予能力、批准自己的操作或验证自己的结果。这些权力始终属于可信控制面。

## 架构

```text
Human / Browser / researchctl
          │ 类型化命令 + 只读 AG-UI 投影
          ▼
Local Control API ───────────────► 可信控制面
                                      │
                         ResearchRun / WorkOrder / Attempt
                         Policy / Approval / Audit / Verifier
                                      │
                                      ▼
                            CollaborationGateway
                                      │
                     Delegation / AgentInvocation / Context
                         ┌────────────┼────────────┐
                         ▼            ▼            ▼
                    internal/HTTP   A2A v1     LangGraph
                         │            │        specialist Agent
                         └────────────┴────────────┘
                                      │
                         Workspace Grant / Lease
                         Git 或 Archive transport
                                      │
                                      ▼
                         Artifact reconciliation
                                      │
                                      ▼
                               独立 Verifier
```

SQLite 记录是唯一权威状态。A2A Task、AG-UI Event、workspace transport handle 和
LangGraph state 都只是适配器或运行时表示，不能取代 `ResearchRun`、`Delegation`、
`AgentInvocation`、`Artifact` 或 `AuditEvent`。

## 已实现能力

- Agent Registry、runtime lease、基于 role/skill/trust-zone 的确定性选择、不可变
  assignment snapshot、Delegation 和 typed AgentInvocation。
- A2A v1 Agent Card、Task/Message/Artifact codec、官方 Python SDK client、tenant
  传递、任务列表、取消、流式聚合，以及通过 `CollaborationGateway` 解码
  `ExecutorResult` 的端到端路径。
- 独立的 Workspace Delegation Plane：grant、路径/分类/大小准入、lease、Git
  worktree 与 Archive transport、artifact-only reconciliation 和 cleanup state。
- ResearchRun、WorkOrder、Attempt、Job 持久化、显式状态机、operation 幂等、取消和
  重启恢复协调。
- 持久化 `RuntimeSession` 和类型化 START/ATTACH/STOP command receipt；PROCESS 与
  REMOTE_HTTP supervisor 使用强进程身份、重启协调，并且不向 Agent 下放生命周期权威。
- 失败关闭的 `researchd` 启动恢复屏障：在接受任何类型化 mutation 前，依次完成
  schema/storage 检查、workspace/worktree 恢复、runtime/job/invocation 协调和 audit
  stream 验证。
- 确定性策略、限定范围的人工审批、分类后的 Agent context 和失败关闭的能力代理。
- 内容寻址 Artifact、provenance、Observation、Claim、独立 Verification、Review 和
  追加式 AuditEvent。
- 数据库分配的单调事件 offset、只读 AG-UI 投影、支持 `Last-Event-ID` 的 SSE
  replay/follow，以及类型化 cancel/approve/human-decision 命令。
- 可选 LangGraph Agent runtime。仓库内的 `agent_research_critic` pilot 会运行真实
  compiled graph 并返回结构化 specialist 结果，同时保持 researchd 的权威性。
- loopback JSON 控制 API、静态 TUI renderer、SQLite backup/restore 检查、运行指标和
  基于 lock file 的 SBOM 生成。

## 环境与安装

- Linux，Python `>=3.12,<3.13`。
- 使用 [`uv`](https://docs.astral.sh/uv/) 安装锁定依赖。
- sandbox/security 测试需要 Bubblewrap。

只安装核心依赖：

```bash
uv sync --frozen
```

安装当前支持的全部 Agent runtime extra：

```bash
uv sync --frozen --extra a2a --extra langgraph-agent
```

这些 extra 不会进入可信 domain/storage/policy 核心。

## 初始化与测试

创建或升级本地控制器数据库：

```bash
uv run alembic upgrade head
```

运行完整软件门禁：

```bash
uv run pytest -q
uv run mypy src tests
git diff --check
```

RC 后的资格验证主线、Gate 策略和可执行证据契约见
[`docs/qualification/`](docs/qualification/README.md)。可用下列命令验证示例记录：

```bash
uv sync --frozen --extra qualification
uv run python scripts/qualification_validate.py \
  --plan examples/qualification_plan.example.json \
  --evidence examples/qualification_evidence.example.json \
  --acceptance examples/qualification_acceptance.example.json
```

直接运行四个互操作/工作区 pilot：

```bash
uv run pytest -q \
  tests/integration/test_protocol_adapters.py \
  tests/integration/test_workspace.py \
  tests/integration/test_agui.py \
  tests/integration/test_langgraph_runtime.py
```

安装 `a2a` extra 后，可运行跨独立进程的 A2A 互操作资格矩阵：

```bash
uv run pytest -q tests/qualification/test_iq01_real_interoperability.py
```

运行 Agent runtime 与 invocation 生命周期资格矩阵：

```bash
uv run pytest -q tests/qualification/test_dq02_runtime_lifecycle.py
```

运行 provider 配置与出境治理软件资格矩阵：

```bash
uv run pytest -q tests/qualification/test_dq03_provider_egress.py
```

运行备份、恢复与灾难恢复软件矩阵：

```bash
uv run pytest -q tests/qualification/test_dq04_backup_restore.py
```

当前格式的备份必须显式绑定不可变候选 commit 和 RC tag；恢复时还必须独立提供预期的
commit/tag。旧快照格式和旧数据库 schema 会被直接拒绝，不做就地升级或兼容补丁。
软件矩阵不能替代 DQ04 验收所需的真实异地存储与 primary-loss 演练。

集成测试是当前可执行 reference workflow。仓库现已提供围绕持久化 RuntimeSession /
Supervisor 的具体 `researchd` composition root 和 CLI；日常 `research` client 现已
覆盖 lifecycle 面（`init`、`status`、交互入口）与首批 shell 命令（`status`、
agent 工作集、`run list`、`task create`/`task cancel`、`msg`、`events watch`、
`approve`、`reject`）；浏览器应用启动入口仍未完成。

嵌入式 composition 必须注册可信服务并使用 `build_startup_barrier(...)`。该屏障先验证
migration `0022` 和实时 DB/CAS 状态，再按冻结顺序调用已有 workspace、worktree、
RuntimeSession、job 和 invocation 恢复路径。任一阶段失败或跳过都会让
`ResearchDaemon` 保持 non-ready；调用方不能用 free-text 或直接 SQL mutation 绕过。

资格探针必须运行在实际部署数据目录，而不是临时替代目录：

```bash
uv run python scripts/dq01_preflight.py --strict --target <deployment-root>
uv run python scripts/dq01_filesystem_probe.py --root <deployment-root>
```

## 查看控制面

先创建一份严格配置。所有路径必须是绝对路径；repository 映射到明确的 Git 根目录，
job type 映射到固定 argv 数组，禁止传入 shell 文本：

```bash
cat > researchd.json <<'JSON'
{
  "database": "/absolute/path/researchd.db",
  "artifact_root": "/absolute/path/artifacts",
  "state_root": "/absolute/path/state",
  "repositories": {"main": "/absolute/path/source-repository"},
  "job_commands": {"typed-check": {"argv": ["/usr/bin/true"]}},
  "host": "127.0.0.1",
  "port": 8788
}
JSON
uv run researchd --config researchd.json validate
uv run researchd --config researchd.json inspect
uv run researchd --config researchd.json init
uv run researchd --config researchd.json serve
curl http://127.0.0.1:8788/api/health
TOKEN=$(tr -d '\n' < /absolute/path/state/control.token)
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8788/api/runs
```

`serve` 不会迁移已有数据库。只有冻结的八阶段恢复屏障全部通过才会进入 READY；
非当前 schema、缺失 repository、未知配置字段、相对路径或非 loopback 监听都会失败关闭。
`job_commands` 为空时会有意禁用 job submission。
`validate` 只解析合同，不接触状态；`inspect` 还会输出配置 SHA256 和非 secret 投影，
固定命令参数只显示数量，不回显内容。

`init` 会在 `<state_root>/control.token` 新建 256-bit、权限为 `0600` 的凭据，并拒绝
覆盖已有凭据。凭据缺失、格式错误、所有者不符或权限不安全时，`serve` 会拒绝启动。
`/api/health` 保留为未认证 liveness/readiness 面；其他 HTTP 读取、stream 和 mutation
都必须携带 `Authorization: Bearer <token>`。该凭据不会进入 SQLite、audit metadata、
snapshot、配置 inspect 或 Agent context。

如果恢复后仍存在未解决的 RuntimeSession、workspace、worktree、job 或 invocation，
`researchd` 只保持运行以暴露 non-ready health 和只读投影；所有经过 daemon 的 mutation
都会继续被拒绝，直到不安全状态被解决。

`researchctl` 会打开已有数据库，但不会构造 Orchestrator：

```bash
uv run researchctl --database researchd.db run list
uv run researchctl --database researchd.db agent list
uv run researchctl --database researchd.db events <run-id> --after <stream-offset>
uv run researchctl --database researchd.db daemon-command list --status ACCEPTED
uv run researchctl --database researchd.db daemon-command resolve <command-id> --resource-ref run_id=<run-id>
```

`daemon-command resolve` 通过命令族专属观察收敛中断遗留的 `ACCEPTED` 回执，
与 HTTP 路由同源；`--abandon` 用于放弃无法判定的结果，`--command-id` 支持幂等重试。

日常 `research` client 经由同一认证 HTTP 面驱动控制面，从不打开数据库：

```bash
uv run research --config researchd.json init
uv run research --config researchd.json status
uv run research --config researchd.json
```

`init` 将 bootstrap 委托给 `researchd init`；`status` 输出一份包含可达性与
就绪状态的 JSON 文档。不带子命令时，`research` 探测 daemon；若无 daemon 可达，
则启动独立于 client 窗口生命周期的 `researchd serve`；退出 shell 不会停止它。
只有 daemon 报告 READY 之后才进入 shell——non-ready daemon 会连同其失败的
启动阶段一起呈现，绝不被绕过。首批 shell 命令提供 `status`、
`agent list`/`agent use`/`agent remove`、`run list`、`task create`、
`task cancel`、`msg`、`events watch`、`approve`、`reject` 与 `quit`；
每条命令都走认证 transport，`agent remove` 仅清除会话本地工作集。

受管 PROCESS Agent invocation 经已安装 Agent 目录动态解析，daemon composition
不固定任何 Agent ID。executor 只有在 runtime lease 有效、存在 HEALTHY
RuntimeSession 且其 launch-profile hash 仍与可信目录一致时才可被选择。
invocation 仅调用 Registry 持有的 loopback endpoint，绝不二次执行启动命令。

不含凭据的 managed coder 安装示例位于
`examples/managed_coder_agent_definition.example.json`。其中绝对可执行文件与
工作目录属于部署配置，安装前应明确调整；client 不能通过 runtime API 提交它们。

如需明确不允许 daemon mutation 的只读投影，可只嵌入 local API：

```bash
uv run python - <<'PY'
from pathlib import Path
from researchd.api.control import LocalControlAPI
from researchd.api.web import serve_local_control
from researchd.storage.db import create_sqlite_engine, session_factory

sessions = session_factory(create_sqlite_engine(Path("researchd.db")))
serve_local_control(LocalControlAPI(sessions)).serve_forever()
PY
```

然后在另一个终端访问：

```bash
curl http://127.0.0.1:8788/api/runs
curl http://127.0.0.1:8788/api/events/<run-id>?after=0
curl http://127.0.0.1:8788/api/runtime-sessions
curl http://127.0.0.1:8788/api/daemon-commands
curl http://127.0.0.1:8788/api/system-events?after=0
curl http://127.0.0.1:8788/api/collaboration-messages/<message-id>
curl -N http://127.0.0.1:8788/api/runs/<run-id>/stream?follow=1
curl -N http://127.0.0.1:8788/api/system-stream?follow=1
```

run event payload 现在携带 `actor_type`/`actor_id`；system stream
（`/api/system-stream`，SSE，支持 `Last-Event-ID`/`after` 续读）与 JSON
`/api/system-events` 投影同源，走同一条单调审计流。按 id 读取协作消息
返回含 classification 的完整记录；AG-UI 流对 `LOCAL_ONLY` 与 `SECRET`
正文的脱敏保持不变。

这个只读启动方式会主动拒绝状态修改。嵌入应用必须使用
`ResearchOrchestrator` 构造 `LocalControlAPI`；类型化命令随后先经过 `ResearchDaemon`
readiness gate，再进入现有策略/状态机。外部请求只携带 `request_version`、`command_id`
和路由特有的意图字段，不得提交 `actor_type` 或 `actor_id`；HTTP adapter 会在服务端绑定
HUMAN 身份，再构造内部命令。接受后的派发返回 `202` 以及带版本的
`DaemonCommandResult` envelope（`command_version`、`command_id`、
`command_type`、`status`、`resource`）：

| 方法 | 路由 | 路由特有 Body 字段 |
|---|---|---|
| `POST` | `/api/agents/{agent_id}/start` | `runtime_id`（可选） |
| `POST` | `/api/runtime-sessions/{runtime_session_id}/stop` | `runtime_id`、`expected_version` |
| `POST` | `/api/runs/{run_id}/cancel` | — |
| `POST` | `/api/work-orders/{work_order_id}/approve` | `grant_id` |
| `POST` | `/api/work-orders/{work_order_id}/human-decision` | `action`（`abort` 或 `revise`；`revise` 必须带 `objective`） |
| `POST` | `/api/work-orders/{work_order_id}/reject` | `approval_id` |
| `POST` | `/api/workspaces` | `workspace_id`、`name` |
| `POST` | `/api/runs` | `workspace_id`、`objective`、`run_id`（可选） |
| `POST` | `/api/collaboration-messages` | `message_id`（`msg_…`）、`run_id`、`purpose`、`body`、`recipient_agent_id`（可选，`agent_…`）、`classification`（可选，默认 `PROJECT_PRIVATE`） |
| `POST` | `/api/backups/create` | `destination`、`candidate_commit`（40 位十六进制）、`candidate_tag`（`vX.Y.Z-rc.…`） |
| `POST` | `/api/backups/verify` | `snapshot` |
| `POST` | `/api/restores/plan` | `snapshot`、`database_destination`、`artifact_destination`、`expected_candidate_commit`、`expected_candidate_tag` |
| `POST` | `/api/daemon-commands/{command_id}/resolve` | `resource_ref`（命令族专属 key/value）、`abandon`（可选） |

`POST /api/agents/{agent_id}/start` 是唯一的公开启动路由：它只接受可选的
`runtime_id`，由 daemon 从受信 launch catalog 解析 launch spec（PROCESS
runtime 启动受监督进程会话，HTTP runtime attach 到已注册端点），并按命令
身份派生 runtime session 身份，使同一命令的重放映射到同一会话。原先任意的
`/api/runtime-sessions/start` 与 `/api/runtime-sessions/attach` 路由已被禁用；
停止会话仍使用上述按会话的 stop 路由。

workspace、research-task、reject 与 collaboration-message 路由经由 orchestrator
控制权威执行，因此要求嵌入应用暴露 `ResearchOrchestrator`；缺失时失败关闭。
`POST /api/work-orders/{work_order_id}/reject` 会把 `WAITING_APPROVAL` 工单与其
pending 审批收敛为 `FAILED`/`REJECTED`（run 以 `APPROVAL_REJECTED` 失败），与策略
拒绝对称。三条备份路由绑定 daemon 自身的数据库与 artifact 根目录：create 生成原子
快照树，verify 只做全量校验、不复制，restore plan 是不写入任何文件的 dry run。
由于 verify 与 plan 不产生持久效果，其中断回执只能被放弃
（`OPERATOR_ABANDONED`），不能被断言为完成。

Migration `0021` 会在派发前预留通用持久化回执。同一命令身份和请求再次到达时，只重放
已完成或已拒绝的结果，不重复执行副作用；用不同输入复用同一身份会被拒绝。若派发中断后
回执仍为 `ACCEPTED`，系统绝不会自动重放；该回执会通过 `/api/daemon-commands` 保持可见，
并阻止 daemon 进入 READY，直到 operator 将其收敛。恢复通道为
`POST /api/daemon-commands/{command_id}/resolve`（或 `researchctl daemon-command resolve`）：
命令族专属 observer 先观察权威状态，operator 只能放弃无法判定的结果
（`OPERATOR_ABANDONED`），不存在自由改写终态的入口。目标回执、resolution 回执与审计
事件在同一事务提交，已终态的目标只能重放、不能被再次解析出不同结果。该路由在 daemon
FAILED 时仍然可达，但仍要求 Bearer token 认证。

本地 token 负责认证 owner client，服务端 actor 绑定则阻止 payload 伪造 attribution；
二者共同构成 PX00 MVP。未来可用 native peer credential 替换 token，而不改变内部命令权威。

Migration `0022` 为已有 `AgentRuntime` 增加一对一、server-owned 的
`RuntimeLaunchProfile`。公共 start/attach 请求不再包含 executable、argv、cwd、endpoint
或 health override。Daemon 会解析 enabled profile、验证 canonical digest、构造内部命令，
并把解析后的 launch-spec snapshot 与 profile hash 一起持久化到 RuntimeSession。PX01
安装命令完成前，profile 只能由可信 in-process 配置服务注册；缺失、禁用、mode 不匹配或
被篡改的 profile 均失败关闭。

系统不存在允许任意 UI event 修改状态的入口。

## 仓库结构

- `src/researchd/collaboration/`：Agent contract、registry、selection、delegation、
  invocation、adapter、message 和 LangGraph runtime。
- `src/researchd/workspace/`：workspace grant、准入、transport、lease、reconciliation
  与 cleanup。
- `src/researchd/orchestrator/`：可信、有界工作流控制器。
- `src/researchd/runtime_sessions/` 与 `supervisor/`：具体 Agent runtime 实例、类型化
  command receipt、副作用 driver 和重启协调。
- `src/researchd/daemon/`：启动恢复屏障和 readiness-gated 类型化 mutation 边界。
- `src/researchd/api/`：本地控制 facade、AG-UI projection、SSE/JSON HTTP 和 TUI。
- `src/researchd/storage/`：权威 SQLAlchemy record 和 Alembic migration。
- `src/researchd/policy/`、`verifier/`、`artifacts/`、`executor/`：可信执行与证据链。
- [`examples/`](examples/) 与 [`schemas/`](schemas/)：版本化 DTO 示例和 JSON Schema。
- [`scripts/`](scripts/)：资格检查、release manifest 与 SBOM helper。
- `tests/`：contract、integration、migration、security 和 typing gate。

## 边界与发布策略

项目只实现并验证当前 contract，不保留旧协议适配层，也不提供旧数据库迁移兼容。
未经验证或不安全的边界会失败关闭。

仓库使用不可变 `v1.0.0-rc.*` Git tag 标识 qualification candidate。Python distribution
在预发布阶段仍保持 `0.1.0`，因此 Git RC tag 用于标识通过资格检查的源码 commit，并与
package semantic version 有意分离。复现候选版本时必须使用最新 tag 及其精确 commit。

项目不承诺普遍意义的分布式 exactly-once，不提供公开 control/A2A service，也尚未完成
交互式 Web/TUI 产品。正式运行批准仍需要绑定精确 commit 的证据，包括绿色 CI、
backup/restore 验证、transport 治理和计划中的 soak/acceptance 检查。
