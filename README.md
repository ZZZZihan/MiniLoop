# MiniLoop

一个用于理解和验证模型工具调用机制的轻量级 Python 内核。项目在受限本地工作区中实现完整的**串行模型—工具执行循环**，支持文件读取（`read_file`）、两阶段乐观锁代码修改（`propose_patch` / `apply_patch`）、固定测试入口执行（`run_tests`）、Deadline 驱动的步数/时间双预算控制，以及 Schema v1.0 脱敏 JSONL 轨迹记录与零副作用只读回放。

## 当前实现状态

| 能力模块 | 状态 | 对应实现与验证证据 |
| --- | --- | --- |
| 模型适配器与安全密钥加载 | 已实现 | `src/miniloop/adapter.py`（`OpenAICompatibleAdapter` + `FakeModelAdapter`）、`src/miniloop/config.py`、`src/miniloop/secrets.py` |
| 工具注册、零黑盒参数 Schema 校验、调用 ID 配对 | 已实现 | `src/miniloop/tools/registry.py`、`src/miniloop/loop.py` |
| 步数、工具次数、墙钟时间预算与明确终态 (`ExitReason`) | 已实现 | `src/miniloop/budget.py`、`src/miniloop/types.py` |
| 受限文件读取、两阶段乐观锁补丁、防篡改测试入口 | 已实现 | `src/miniloop/tools/read_file.py`、`src/miniloop/tools/patch.py`、`src/miniloop/tools/run_tests.py` |
| Schema v1.0 脱敏 JSONL 轨迹与纯只读回放 | 已实现 | `src/miniloop/trajectory.py`（回放过程模型与工具调用计数均为 `0`） |
| 10 类确定性协议验证套件 (`S01`–`S10`) | 已实现（`10 / 10` 通过） | `src/miniloop/protocol_verify.py`、`miniloop verify-protocol --check` |
| 冻结代码修复案例集与真实模型双轨评测 | 已实现（`dev` `2 / 2`，`test` `4 / 4`） | `cases/`、`src/miniloop/cases.py`、`src/miniloop/eval_runner.py`、[评测报告](docs/status/evaluation-report.md) |

### 明确边界与非目标（未实现项）

首版严格聚焦于**单模型适配器 + 单串行执行循环 + 本地隔离工作区**：
- **不混为一谈**：确定性协议验证（`fake`，零网络）证明内核协议与边界控制按约定工作；真实模型任务评测（`openai_compatible`）仅反映特定模型在仓库内 6 个冻结小型 Python 案例上的表现，不代表通用生产可靠性或外部大型榜单成绩。
- **未实现范围**：暂不实现多智能体编排、上下文自动压缩、跨进程中断恢复（轨迹回放 `replay` 仅为只读审计，不是断点续跑）、容器/VM 级操作系统沙箱或 Web UI。

---

## 安装与环境准备

需要 Python `>= 3.12` 与 `uv`（或标准 `pip`）。运行时仅依赖 `httpx>=0.27` 直接构造与解析 OpenAI 兼容 `/v1/chat/completions` HTTP JSON 报文：

```bash
uv sync --extra dev
uv run python -c "import miniloop; print(miniloop.__version__)"
```

---

## 快速开始与验证命令

### 1. 一键离线门禁验证（零网络、零密钥）

依次执行 `ruff`、`mypy --strict`、`pytest`（含 `cases/MANIFEST.json` SHA-256 校验与 6 个案例人工参考补丁自检）以及 10 类确定性协议验证场景（`S01`–`S10`）：

```bash
./scripts/verify.sh --offline
```

也可以单独运行 10 类确定性协议验证套件：

```bash
uv run python -m miniloop.cli verify-protocol --check
```

### 2. 真实模型只读循环（P0-01）

默认对接本机 OpenAI 兼容网关（`http://127.0.0.1:3425/v1`），密钥按 `MINILOOP_MODEL_API_KEY` → `MINILOOP_MODEL_API_KEY_FILE` → `~/.config/miniloop/magpie.key` → `~/.config/lab-agent/magpie.key` 顺序加载。真实模式下若缺少模型名或密钥会立即报 `ConfigError` 退出，**绝不静默回退到 `fake`**：

```bash
uv run python -m miniloop.cli run \
  --provider openai_compatible \
  --base-url http://127.0.0.1:3425/v1 \
  --model remote-magpie/auto/claude-sonnet-5-5 \
  --workspace ./workspaces/demo_readonly \
  --task "Please read README_DEMO.md and config_sample.ini in the workspace using read_file, and report the service codename, maintenance window, and listen_port." \
  --output /tmp/p0_01_real_run.jsonl
```

一键运行离线检查 + 真实模型冒烟验证：

```bash
./scripts/verify.sh
```

### 3. 纯只读轨迹回放（P1-01）

回放器仅解析 JSONL 文件并核验序列号连续性、`call_id` 配对闭合与终态事件 `run_end`，不发起任何网络请求、不执行任何工具：

```bash
uv run python -m miniloop.cli replay /tmp/p0_01_real_run.jsonl
```

### 4. 运行冻结代码修复评测（P0-04 / P1-02）

在隔离工作区副本上运行 `dev`（2 例）、`test`（4 例）或 `all`（全部 6 例），由独立评分器复核测试文件 SHA-256 完整性、修改文件白名单与独立 `pytest` 退出码（严格区分循环正常退出 `exit_reason="completed"` 与代码修复成功 `repair_succeeded=True`）：

```bash
uv run python -m miniloop.cli eval \
  --split all \
  --provider openai_compatible \
  --base-url http://127.0.0.1:3425/v1 \
  --model remote-magpie/auto/claude-sonnet-5-5 \
  --output-dir runs/eval_all_claude_sonnet_5_5 \
  --report-out docs/status/evaluation-report.md
```

---

## 文档与验收报告

- [架构与边界](docs/architecture.md)：模块职责、消息流、错误分层（`ToolError` vs `ProtocolError`）与非目标。
- [P0 / P1 实施清单](docs/roadmap.md)：各项任务完成状态、对应模块、运行命令与证据路径。
- [验证与评测协议](docs/evaluation.md)：确定性替身与真实模型双轨验证规则、防作弊约束与指标口径。
- [P0-01 真实模型首跑报告](docs/status/p0-01-first-real-loop.md)：首个真实只读工具调用循环与轨迹回放记录。
- [双轨验证与真实模型修复评测报告](docs/status/evaluation-report.md)：10/10 确定性协议验证表 + 6 个冻结修复案例逐例真实模型评测结果、Token 用量、Diff 与乐观锁错误恢复轨迹。
- [开源参考与归属](REFERENCES.md)：学习来源、概念对照与本人独立实现说明。

## 归属与许可证

本项目所有 Python 源码、工具 Schema 校验器、乐观锁补丁机制、轨迹记录/回放器及 6 个冻结修复案例均为独立编写，未复制上游代码。**本项目许可证尚未选定**；参考项目的许可证不自动成为本项目许可证。
