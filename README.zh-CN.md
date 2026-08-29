# Research Agent System（中文版）

[English](README.md)

Research Agent System 是一个 **Agent Collaboration Plane + Trusted Control
Plane（Agent 协作面 + 可信控制面）**，面向长时间研究任务。平台接入的边界是
Agent，而不是模型 API；模型、provider、协议和运行位置都只是 AgentRuntime 的
实现细节。控制器把 Agent 提案转化为持久化、受策略约束、可审计、可恢复的执行
流程；Agent 不能直接修改工作流状态、执行任意主机命令，也不能自行证明研究结果成立。

## 项目定位

这是一个 Python 3.12 模块化单体，核心是可信控制器：

```text
Human / researchctl
        │
        ▼
Agent 协作面
Registry / Runtime / Delegation / Invocation / Context
        │
        ▼
可信控制面
Run / WorkOrder / Attempt / Policy / Approval / Verification / Audit
        │
        ▼
Agent Runtime 适配器
internal / process / HTTP / A2A
```

Agent 提出计划、承担工作单并返回审查意见。控制器负责状态转换、能力授权、数据出境
分类、审批、验证和最终接受。A2A/MCP 与模型 provider 都是可替换边界，不能取代
ResearchRun、WorkOrder、Attempt、Job、Verification 和 AuditEvent 等权威记录。

协作面提供持久化的 `AgentProfile`、`AgentRuntime`、`Delegation` 和
`AgentInvocation`。每次计划、执行和审查都归属到具体 Agent，并保留运行时快照；
Agent skill 只是描述信息，可信 Capability 只能由策略系统授予。

## 已实现功能

- ResearchRun/WorkOrder/Attempt/Job 持久化、显式状态转换和版本控制。
- Agent Registry、runtime 健康租约、确定性选择、Delegation、typed
  AgentInvocation 和追加式协作消息。
- internal、local-process、HTTP、A2A 的统一适配器；协议任务不会取代控制面权威记录。
- 崩溃感知的 Job 提交、operation-id 幂等、取消和重启恢复协调。
- Bubblewrap 无网络执行、环境清理、能力代理、worktree 隔离和有界资源限制。
- 内容寻址 Artifact、来源校验、独立验证和追加式审计事件。
- 确定性策略、人工审批、有界 runtime 调用，以及针对临时适配器故障的分类重试。
- SQLite 在线备份、CAS 引用一致性、校验和及恢复健康检查。
- 工作流指标、SQLite/WAL/CAS 增长和备份新鲜度观测。

## 快速开始

```bash
uv sync --frozen
uv run alembic upgrade head
uv run pytest -q
uv run mypy src tests
uv run researchctl --database researchd.db agent list
```

当前仓库提供库、loopback 本地控制 API 和只读 `researchctl` 命令。集成测试是可执行的
reference workflow；控制面提供 Agents、Runs、Delegations、Approvals、Artifacts
以及事件/时间线查询。修改状态的命令必须显式接入控制器实例。

示例 DTO 和 JSON Schema 位于 [`examples/`](examples/) 与 [`schemas/`)；可执行的
资格辅助工具位于 [`scripts/`](scripts/)。发布 manifest、运行态数据库和资格证据
不会写入源码基线。

## 发布状态

仓库通过不可变的 `v1.0.0-rc.*` tag 发布候选版本，具体版本以最新 Git tag 为准。控制面及已审查的软件安全措施已经实现，但本仓库
不宣称已获得生产 Go。正式部署前仍需收集目标环境的 runtime/transport 治理、异机备份/恢复和
长时间运行 soak 证据，并完成最终部署决策。

## 明确边界

项目不承诺普遍意义上的分布式 exactly-once，不提供公开 A2A/MCP server，也不会在
Agent 之间隐式 fallback。未验证或不安全的边界会失败关闭，并要求显式部署决策。
