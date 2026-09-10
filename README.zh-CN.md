# Research Agent System（中文版）

[English](README.md)

> **状态。** 当前活动方向是 Rust v2 重写：原生 Coding Agent 与异构 Agent 团队层，
> 分支 `v2/rust-agent-team`。Python `researchd` 控制面实现是冻结参考，不再是产品。
> R4 已封板；R5 仅有本地实现，仍在审校，尚未验收。见
> [路线图](docs/v2/ROADMAP.md) 与 [R5 状态](docs/v2/R5_STATUS.md)。

Research Agent System 是一个**异构 Agent Coding/Work 团队**。

核心目标只有一个：把不同能力和成本的 Agent 接入同一个项目。高智能 Agent 负责规划、
困难推理、架构、综合与审阅；本地或低成本 Agent 与确定性 Worker 负责重复、耗时、
文件密集、数据密集和工具密集型工作。结果与工件自动回流给需要继续推理的 Agent，
全程无需人工复制粘贴。

通信、调度、恢复与安全边界是让多个 Agent 完成任务的支撑机制，不是产品本身。

## 架构

```text
User
  |
  v
Team Session
  |
  v
Lead / Reasoning Agent
  | delegate
  +------------------+-------------------+
  v                  v                   v
Reasoning Agent    Local Model Agent   Utility Worker
  |                  |                   |
  +----- result / files / messages ------+
                       |
                       v
              Lead integrates result
                       |
                       v
                    Deliver
```

每个原生 model-backed Agent 内部跑同一套循环：

```text
Init -> Observe -> Model Decision -> Tool Execution -> Observe -> ...
     -> Verify -> Deliver / Rollback
```

## 原生 Coding Agent

原生 Rust Coding Agent 是可恢复的工具调用运行时，提供五个原子工具：

- `view_file`：workspace 内路径、分页、返回文件哈希。
- `edit_file`：精确唯一匹配、预期文件哈希、有限的换行/行尾空白归一、原子写、语法守卫并回滚。
- `write_file`：新文件或显式短文件替换，带大小上限。
- `search_dir`：有界的路径/行/命中记录，不返回整文件。
- `execute_command`：默认结构化 `program + argv + cwd + timeout + env`，带进程组终止与输出截断。

可靠性机制（路径限制、命令超时、worktree 隔离、输出截断、原子写、回滚）保留，因为它们
让 Coding Agent 可靠。它们是运行时机制，不是控制面产品。

## 团队层

团队层只负责拆分工作和在 Agent 之间搬运结果，不会变成企业级工作流引擎。Agent 有层级
（`Reasoner`、`Worker`、`Utility`）、driver 和并发上限。路由是确定性的：推理/审阅走
Reasoner，批量/工具型工作走 Worker 或 Utility，显式指定优先，否则用配置的默认。Worker
结果自动成为其父任务、Lead 和被显式指名的 Agent 的上下文。

计划中的 Driver（R6，尚未实现）：

- `NativeCodingAgentDriver`：Rust 状态机 + 模型客户端 + 五个工具。
- `ExternalCliAgentDriver`：运行外部 Coding Agent CLI，不再包一层工具循环。
- `UtilityDriver`：测试/构建/搜索/批处理的确定性 Worker。

## 路线图

活动计划是 [docs/v2/ROADMAP.md](docs/v2/ROADMAP.md) 里的 `R0 -> R8`：

- R0 方向重置（本次重定位）
- R1 Rust 核心（workspace、状态机、SQLite 日志、模型 trait、恢复）
- R2 工具与工作区
- R3 上下文预算与恢复
- R4 单 Agent E2E
- R5 团队调度
- R6 真实 Agent（高智能 + 本地 Qwen）
- R7 TUI 切换
- R8 删除遗留

两条阻塞 E2E：单个原生 Agent 在小而真实的 Git 仓库里检查、改缺陷、跑检查、自纠并交付
补丁；以及团队场景，Lead 委派至少两个任务，本地/utility Worker 执行，结果与工件回流，
Lead 据此产出最终答案。

## Rust 开发

```bash
cargo fmt --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

## 遗留 Python（冻结参考）

`src/researchd/` 下的 Python 包（守护进程 `researchd`、`researchctl`、`research`）是冻结
参考实现。它仍能构建与测试，但不是活动方向。不要扩展它。R8 会在 Rust E2E 对齐后删除
不可达的控制面模块。

遗留关键命令：

```bash
uv sync --frozen
uv run pytest -q
uv run mypy src tests
git diff --check
```

完整的遗留运维细节（控制 API 路由、daemon 命令、Browser Control Tower、TUI、
backup/restore）保存在冻结基线 `8cf27dc2a9e03ffbc1fbd091a576e0fb0f16bb93` 与 git 历史中。
遗留的 qualification 框架在 `docs/qualification/`，仅作历史参考。

## 许可证

Apache License 2.0 (ALv2)，详见根目录 `LICENSE` 文件。
