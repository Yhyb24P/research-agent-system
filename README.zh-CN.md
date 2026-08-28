# Research Agent System（中文版）

[English](README.md)

Research Agent System 是一个面向长时间研究任务的可信控制面。它把模型提出的
计划转化为持久化、受策略约束、可审计、可恢复的执行流程；模型不能直接修改
工作流状态、执行任意主机命令，也不能自行证明研究结果成立。

## 项目定位

这是一个 Python 3.12 模块化单体，核心是可信控制器：

```text
用户 / CLI
   │
   ▼
可信控制器 ── SQLite WAL + 审计轨迹
   ├── 有界编排器
   ├── 策略、预算和人工审批
   ├── 本地执行器 ── Bubblewrap 沙箱
   ├── 独立验证器 ── 内容寻址 Artifact
   └── 模型 / provider 适配器
```

模型只能提出计划、工作单和审查意见。控制器负责状态转换、能力授权、数据出境
分类、审批、验证和最终接受。A2A/MCP 与模型 provider 都是可替换边界，不能取代
ResearchRun、WorkOrder、Attempt、Job、Verification 和 AuditEvent 等权威记录。

## 当前部署拓扑

当前通过 `aweswitch qw` 启动 Qwen agent，由远程 workstation 推理节点承担模型推理：

```text
本机：控制面 + agent client ──调用──> 远程 Qwen workstation：推理 + GPU
```

- 本机不加载模型权重，也不负责推理，因此控制面不需要 GPU。
- GPU 和模型权重属于远程 Qwen 推理节点。
- 仓库另有 loopback-only 的 `VLLMLocalModel`，用于同机 vLLM 服务；这不是当前
  远程 Qwen 主路径。
- 远程推理 endpoint 的传输、鉴权和 provider 策略需要单独审查，不能自动视为本地
  GPU 执行。

## 已实现功能

- ResearchRun/WorkOrder/Attempt/Job 持久化、显式状态转换和版本控制。
- 崩溃感知的 Job 提交、operation-id 幂等、取消和重启恢复协调。
- Bubblewrap 无网络执行、环境清理、能力代理、worktree 隔离和有界资源限制。
- 内容寻址 Artifact、来源校验、独立验证和追加式审计事件。
- 确定性策略、人工审批、云调用预算、成本核算，以及针对超时/429/5xx 的分类有界重试。
- 可选的 GPU durable admission lease；后端无法执行硬件约束时失败关闭。
- SQLite 在线备份、CAS 引用一致性、校验和及恢复健康检查。
- 工作流指标、SQLite/WAL/CAS 增长和备份新鲜度观测。

## 快速开始

```bash
uv sync --frozen
uv run pytest -q
uv run mypy src tests
uv run alembic upgrade head
```

示例 DTO 和 JSON Schema 位于 [`examples/`](examples/) 与 [`schemas/`)；可执行的
资格辅助工具位于 [`scripts/`](scripts/)。发布 manifest、运行态数据库和资格证据
不会写入源码基线。

## 发布状态

仓库通过不可变的 `v1.0.0-rc.*` tag 发布候选版本，具体版本以最新 Git tag 为准。V1 控制面及已审查的软件安全措施已经实现，但本仓库
不宣称已获得生产 Go。正式部署前仍需收集目标环境的 provider 治理、异机备份/恢复和
长时间运行 soak 证据，并完成最终部署决策。

只有在启用本地 GPU Job 或同机 vLLM 时才需要执行 GPU 硬件资格；当前远程 Qwen 控制面
拓扑不要求本机 GPU。

## 明确边界

项目不承诺普遍意义上的分布式 exactly-once，不提供公开 A2A/MCP server，不会因本地
模型失败而自动切换云模型，也不会仅凭逻辑 admission lease 宣称硬件 GPU 隔离。未验证
或不安全的边界会失败关闭，并要求显式部署决策。
