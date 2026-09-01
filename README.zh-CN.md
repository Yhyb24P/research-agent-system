# Research Agent System（中文版）

[![control-plane-quality](https://github.com/Yhyb24P/research-agent-system/actions/workflows/quality.yml/badge.svg)](https://github.com/Yhyb24P/research-agent-system/actions/workflows/quality.yml)

[English](README.md)

> **开发者预览 / 早期体验（Developer Preview / Early Access）**：可信边界保持
> 不变，但生产级资格认证仍在进行中。本版本不是 Production Go 发布；详见
> [docs/qualification/CANDIDATE_RELEASE_CONTRACT.md](docs/qualification/CANDIDATE_RELEASE_CONTRACT.md)。

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
- loopback JSON 控制 API、Browser Control Tower、交互式 TUI、SQLite backup/restore 检查、运行指标和
  基于 lock file 的 SBOM 生成。

## 环境与安装

- Linux，Python `>=3.12,<3.13`。
- 使用 [`uv`](https://docs.astral.sh/uv/) 安装锁定依赖。
- sandbox/security 测试需要 Bubblewrap。
- 沙箱执行需要 `/workspace` 宿主目录。
- prlimit 工具用于资源限额（通常在 `/usr/bin/prlimit`）。


只安装核心依赖：

```bash
uv sync --frozen
```

安装当前支持的全部 Agent runtime extra：

```bash
uv sync --frozen --extra a2a --extra langgraph-agent
```

这些 extra 不会进入可信 domain/storage/policy 核心。

## Developer Preview 快速启动

发布渠道只允许从不可变 HTTPS manifest 安装：

```bash
curl -fsSL https://<product-domain>/install-preview.sh | \
  sh -s -- --manifest https://<product-domain>/<release>/preview-manifest.json
```

安装器会验证 wheel SHA-256，创建版本化的用户自有环境，并且只暴露
`~/.local/bin/research`。可变分支、非 HTTPS 工件、非 Preview manifest 或已存在的安装
目标都会被拒绝。manifest 合同示例见
[`examples/preview_install_manifest.example.json`](examples/preview_install_manifest.example.json)。
发布操作者只能从已提交且 tracked 工作树干净的版本生成正式 manifest：

```bash
uv build
uv run python scripts/preview_manifest.py \
  --wheel dist/<preview-wheel>.whl \
  --wheel-url https://<product-domain>/<release>/<preview-wheel>.whl \
  --source-candidate-commit ca67f55acf95afd114e5af3059bd224ce45adf29 \
  --source-candidate-tag v1.0.0-rc.82 \
  --output dist/preview-manifest.json
```

进入你希望 Agent 操作的 Git 项目，然后启动日常客户端：

```bash
uv sync --frozen --extra tui
uv run research
```

首次启动时，`research` 会创建仅属主可读写的全局配置，初始化控制器，通过认证 API
创建本地项目 workspace，启动 `researchd` 并打开 TUI。系统不会隐式信任仓库内的配置
文件。可用以下命令检查安装并接入 Agent，诊断信息不会回显凭据：

```bash
uv run research doctor
uv run research agent profiles
uv run research agent add planner
uv run research agent add coder
uv run research agent add reviewer
```

如果本机仅发现一个受支持的 aweswitch profile，`agent add` 会自动选择；否则显式传入
`--profile aweswitch:<profile>`。首个 bridge 支持 Qwen profile，并会按 managed Agent
JSON 合同校验每次响应。TUI 中可使用 `/task <目标>`、`/msg @agent <消息>` 和
`/attach <文件> [--to @agent]`、`/approve <approval-id>`。附件内容有大小上限和明确
分类，经内容寻址后关联到当前 Run；Agent 只能取得策略允许的 Artifact 上下文，绝不会
收到宿主绝对路径。关闭 TUI 或独立 console 不会停止 daemon 或 Agent runtime。

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
Supervisor 的具体 `researchd` composition root 和 CLI。daemon 自己持有的编排
driver 会在启动时重新扫描可运行的持久化 run，并经可信控制器推进。日常 `research`
client 覆盖初始化、daemon 的 status/stop/restart、以 workspace 为焦点的交互 shell、
可分离的投影 console 与 Browser Control Tower。

嵌入式 composition 必须注册可信服务并使用 `build_startup_barrier(...)`。该屏障先验证
migration `0026` 和实时 DB/CAS 状态，再按冻结顺序调用已有 workspace、worktree、
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
# 仅在 daemon 停止时执行；会创建带时间戳的迁移前备份。
uv run researchd --config researchd.json migrate
# 仅在 daemon 停止时执行；definition 是可信的本地管理员输入。
uv run researchd --config researchd.json install-agent agent_definition.json
uv run researchd --config researchd.json serve
curl http://127.0.0.1:8788/api/health
TOKEN=$(tr -d '\n' < /absolute/path/state/control.token)
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8788/api/runs
```

`serve` 不会迁移已有数据库。`migrate` 是显式维护操作：缺少数据库或 daemon 正在
运行时都会拒绝，先复制出带时间戳的迁移前备份，再升级仓库打包的 migration chain。
`install-agent` 同样只能在 daemon 停止时由本地管理员运行；它校验并安装可信的
`AgentDefinition`，日常 client 无法提交 runtime 启动细节。只有冻结的八阶段恢复屏障全部通过才会进入 READY；
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
uv run research --config researchd.json daemon status
uv run research --config researchd.json daemon stop
uv run research --config researchd.json daemon restart
uv run research --config researchd.json browser
uv run research --config researchd.json
```

`init` 将 bootstrap 委托给 `researchd init`；`status` 输出一份包含可达性与
就绪状态的 JSON 文档。不带子命令时，`research` 探测 daemon；若无 daemon 可达，
则启动独立于 client 窗口生命周期的 `researchd serve`；退出 shell 不会停止它。
只有 daemon 报告 READY 之后才进入 shell——non-ready daemon 会连同其失败的
启动阶段一起呈现，绝不被绕过。shell 提供 `status`；`workspace list`/
`workspace create`/`workspace use`；`agent list`/`agent use`/`agent remove`/
`agent start`；`runtime list`/`runtime stop`；`run list`；`task create`/
`task cancel`；`msg`；`handoff list`/`handoff accept`/`handoff reject`；
`events watch`；`approval approve <approval-id>`；remote 的 attach/renew/detach；以及
`quit`。已 focus workspace 时，`task create <objective>` 使用当前 workspace；否则第一个
参数必须是 workspace ID。每条命令都走认证 transport，`agent remove` 仅清除会话本地工作集。

`research browser` 会打开只监听 loopback 的 Browser Control Tower。HTML、CSS 和
JavaScript 本身不包含控制器状态或凭据；日常 client 把本机已有凭据放入 URL fragment
（HTTP 永远不会发送 fragment），页面会立即清除该 fragment，只在内存保留凭据。所有
读取和类型化命令仍通过同一认证控制 API；浏览器布局与刷新状态不会成为权威状态。
页面提供按 run 的 Collaboration Window、按 Agent 的 Console，以及从内存 offset
续传的 system-event stream。

远端 attach 不等同于本地 runtime session。`remote attach <runtime-id>` /
`remote renew <runtime-id>` / `remote detach <runtime-id>` 只能引用已安装的 A2A runtime；daemon 从 Registry
解析 endpoint、protocol 与 tenant，并持有可续期 lease，client 不能提交这些值。

受管 PROCESS Agent invocation 经已安装 Agent 目录动态解析，daemon composition
不固定任何 Agent ID。executor 只有在 runtime lease 有效、存在 HEALTHY
RuntimeSession 且其 launch-profile hash 仍与可信目录一致时才可被选择。
invocation 仅调用 Registry 持有的 loopback endpoint，绝不二次执行启动命令。

不含凭据的 managed coder 安装示例位于
`examples/managed_coder_agent_definition.example.json`。其中绝对可执行文件与
工作目录属于部署配置，安装前应明确调整；client 不能通过 runtime API 提交它们。
`research-coder-agent` 参考进程实现对应的无凭据 loopback turn protocol。
它只能提出类型化 action；`researchd` 经 `CapabilityBroker` 执行已授权 action，
并由控制面构造权威 `ExecutorResult`。

CollaborationMessage 使用封闭 purpose 集合（`DISCUSSION`、`STATUS`、
`QUESTION`、`DIRECTIVE`、`NOTICE`），并可持久关联同一 run 内的 WorkOrder、
Delegation、Invocation 或前序消息。这些关联只提供沟通上下文，不授予工作流权威。
认证后的 `msg` 命令可用 `--reply-to`、`--delegation` 或 `--invocation`
添加关联。请求 schema 不包含 sender 身份；daemon 将其绑定为已认证的本地 human。
原生 collaboration read model 支持按 message ID 查询及
`GET /api/runs/<run_id>/messages` 列表，run timeline 同步携带持久的
reply/delegation/invocation 关联。展示投影会遮蔽 `LOCAL_ONLY` 与 `SECRET` 正文。

安装可选 `tui` extra 后，可运行 `research --config researchd.json tui` 打开
包含 Collab、Agents、Tasks、Approvals、System 的只读投影工作区。刷新与布局
状态仅存在于 client；TUI 不直连数据库，也不承载业务逻辑。

同一个 daemon 也可由相互独立的终端 client 观察：

```bash
research --config researchd.json console collab --run run_example
research --config researchd.json console agent agent_coder --run run_example
research --config researchd.json console system
```

每个 console 都是可随时关闭的投影 client；关闭其中一个不会停止 Agent、runtime
session 或 daemon。

managed turn request 携带 canonical invocation purpose 与结构化 payload：执行
turn 接收由 controller 构造的 local request，规划与评审 turn 返回由 controller
验证的结构化业务输出。Agent-origin message 与 handoff proposal 统一进入
`AgentActionBroker`；其 action 不携带调用方可指定的 authority scope，Broker 从
live Invocation 推导 sender、run、WorkOrder、Delegation、Agent 与 runtime，
并拒绝过期 lease 或失效 ownership。handoff 只能来自 execution-scoped invocation。

Handoff 是独立、非权威的 `HandoffProposal`，绝不是 message text。Agent 可经
同一个 invocation-bound Broker 提出 `CONTINUE` 或 `REVISE`；source 身份与
scope 均由控制面派生，引用的 artifact/observation 必须属于同一 run。
只有 Controller 才能接受 proposal。

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
| `POST` | `/api/approvals/{approval_id}/approve` | — |
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

workspace、research-task、approval、reject 与 collaboration-message 路由经由 orchestrator
控制权威执行，因此要求嵌入应用暴露 `ResearchOrchestrator`；缺失时失败关闭。
approval request 只是 HUMAN 意图：路由在服务端绑定本地 HUMAN actor，由可信控制面
派生或复用限定范围的 grant、消费 one-shot authority，并原子恢复关联的 WorkOrder 与
ResearchRun。client 永远不能提交 grant ID 来批准。
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

Migration `0022` 为已安装的 `AgentRuntime` 增加一对一、server-owned 的
`RuntimeLaunchProfile`。公共 start/attach 请求不再包含 executable、argv、cwd、endpoint
或 health override。Daemon 会解析 enabled profile、验证 canonical digest、构造内部命令，
并把解析后的 launch-spec snapshot 与 profile hash 一起持久化到 RuntimeSession。
Agent definition 与 profile 只通过 stopped-daemon 的可信安装路径进入；缺失、禁用、
mode 不匹配或被篡改的 profile 均失败关闭。

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

`v1.0.0-rc.80` 是不可变的历史 qualification 证据，不能为后续产品化代码背书。RC tag
只冻结候选身份；它在 candidate qualification **之前**创建，使 IQ/DQ/RQ 证据能够绑定精确源码
工件。Gate 通过、生产批准和 GitHub Release 都是后续的独立决定。

Git tag `vX.Y.Z-rc.N` 必须精确映射到 Python distribution `X.Y.ZrcN`，最终 `vX.Y.Z` tag
映射到 `X.Y.Z`。`v1.0.0-rc.81` / `1.0.0rc81` 对应
`f7785244acc0687324376806666ead2be26bf478`，是不可移动、不可复用且未签名的历史
product-candidate snapshot；其 qualification 尚未建立，也没有 GitHub Release。下一个冻结候选为
`v1.0.0-rc.82` / `1.0.0rc82`；其 tag 只冻结候选身份，不代表 qualification 或发布状态。详见[候选与发行合同](docs/qualification/CANDIDATE_RELEASE_CONTRACT.md)。

项目不承诺普遍意义的分布式 exactly-once，也不提供公开 control/A2A service。正式运行批准仍需要绑定精确 commit 的证据，包括绿色 CI、
backup/restore 验证、transport 治理和计划中的 soak/acceptance 检查。

## 许可证

Apache License 2.0 (ALv2)，详见根目录 `LICENSE` 文件。
