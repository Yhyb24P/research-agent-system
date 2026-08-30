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
Supervisor 的具体 `researchd` composition root 和 CLI；日常 `research` client 与浏览器
应用启动入口仍未完成。

嵌入式 composition 必须注册可信服务并使用 `build_startup_barrier(...)`。该屏障先验证
migration `0020` 和实时 DB/CAS 状态，再按冻结顺序调用已有 workspace、worktree、
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
```

`serve` 不会迁移已有数据库。只有冻结的八阶段恢复屏障全部通过才会进入 READY；
非当前 schema、缺失 repository、未知配置字段、相对路径或非 loopback 监听都会失败关闭。
`job_commands` 为空时会有意禁用 job submission。
`validate` 只解析合同，不接触状态；`inspect` 还会输出配置 SHA256 和非 secret 投影，
固定命令参数只显示数量，不回显内容。

如果恢复后仍存在未解决的 RuntimeSession、workspace、worktree、job 或 invocation，
`researchd` 只保持运行以暴露 non-ready health 和只读投影；所有经过 daemon 的 mutation
都会继续被拒绝，直到不安全状态被解决。

`researchctl` 会打开已有数据库，但不会构造 Orchestrator：

```bash
uv run researchctl --database researchd.db run list
uv run researchctl --database researchd.db agent list
uv run researchctl --database researchd.db events <run-id> --after <stream-offset>
```

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
curl http://127.0.0.1:8788/api/system-events?after=0
curl -N http://127.0.0.1:8788/api/runs/<run-id>/stream?follow=1
```

这个只读启动方式会主动拒绝状态修改。嵌入应用必须使用
`ResearchOrchestrator` 构造 `LocalControlAPI`；类型化命令随后先经过 `ResearchDaemon`
readiness gate，再进入现有策略/状态机。所有请求体都必须携带命令身份字段
`command_id`、`actor_type`（`HUMAN`/`SYSTEM`）和 `actor_id`；接受后的派发返回 `202`
以及带版本的 `DaemonCommandResult` envelope（`command_version`、`command_id`、
`command_type`、`status`、`resource`）：

| 方法 | 路由 | 路由特有 Body 字段 |
|---|---|---|
| `POST` | `/api/runs/{run_id}/cancel` | — |
| `POST` | `/api/work-orders/{work_order_id}/approve` | `grant_id` |
| `POST` | `/api/work-orders/{work_order_id}/human-decision` | `action`（`abort` 或 `revise`；`revise` 必须带 `objective`） |

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
