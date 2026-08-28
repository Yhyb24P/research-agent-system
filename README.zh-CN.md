# Research Agent System（中文版）

本仓库实现一个可信的本地研究控制面：模型提出计划，控制面负责状态和策略，
本地或远程执行器执行任务，独立验证器确认结果，人类负责必要授权。

## 当前状态

当前版本已完成 TASK00–TASK08 以及后续资格加固，最新候选版本为
`v1.0.0-rc.41`。状态是：

> V1 控制面已完成；部署资格验证仍在进行。

这不是“已获得生产 GO”声明。生产发布仍需目标部署环境中的云服务、备份恢复和
长时间 soak 证据，并通过 DQ06 Go/No-Go 审查。

## 当前推理拓扑

当前使用 `aweswitch qw` 启动 Qwen agent：

```text
本机控制面 / agent client  ──调用──> 远程 Qwen workstation 推理节点
                                      └─加载模型并使用 GPU
```

- 本机不加载模型权重，也不负责推理，因此本机控制面不需要 GPU。
- GPU 资源属于远程 Qwen 推理节点。
- 本仓库另有 loopback-only 的 `VLLMLocalModel`，用于同机 vLLM 服务；这不是当前
  `aweswitch qw` 远程推理路径。
- 远程推理 provider 的传输、鉴权、留存策略仍需按 DQ03 归档证据，不能因为模型
  已能调用就自动视为生产资格通过。

## 已实现的安全边界

- SQLite WAL 持久化、幂等 Job 和恢复协调。
- Bubblewrap 无网络沙箱、能力代理和文件系统竞态防护。
- 内容寻址 Artifact、独立验证和审计事件。
- 云调用预算、429/5xx 有界重试及无 fallback 语义。
- GPU Job 的逻辑准入、独占 lease、丢失任务保持和显式释放。
- 备份快照、CAS 引用一致性、校验和和恢复健康检查。

## 验证与资格工具

```bash
uv sync --frozen
uv run pytest -q
uv run mypy src tests
uv run alembic upgrade head

# 发布 provenance
.venv/bin/python scripts/release_manifest.py --output release-manifest.json

# DQ01 主机预检
.venv/bin/python scripts/dq01_preflight.py --strict --output dq01-preflight-evidence.json
```

DQ00–DQ06 的详细要求、证据格式和当前 Go/No-Go 判断见：

- [DQ00 发布基线](docs/DQ00_RELEASE_BASELINE.md)
- [DQ01 沙箱资格](docs/DQ01_SANDBOX_QUALIFICATION.md)
- [DQ02 GPU/推理资格](docs/DQ02_GPU_JOB_QUALIFICATION.md)
- [DQ03 云服务资格](docs/DQ03_CLOUD_QUALIFICATION.md)
- [DQ04 备份与灾备资格](docs/DQ04_BACKUP_DR_QUALIFICATION.md)
- [DQ05 运行与 soak 资格](docs/DQ05_OPERATIONAL_QUALIFICATION.md)
- [DQ06 生产 Go/No-Go](docs/DQ06_PRODUCTION_GO_NO_GO.md)

英文说明见 [README.md](README.md)。
