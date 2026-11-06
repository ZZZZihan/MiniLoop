# MiniLoop 实施清单

本文件记录 MiniLoop **P0（最小可执行内核）** 与 **P1（可检查轨迹与可信交付）** 的完成状态、对应实现模块、可复现运行命令与验收证据路径。

## P0：能运行、能解释的最小内核

### P0-01 最小真实工具调用循环（首个任务）

- [x] 选择一个真实模型及单个适配器，记录配置和依赖版本（`src/miniloop/config.py`、`src/miniloop/secrets.py`、`src/miniloop/adapter.py:OpenAICompatibleAdapter`，依赖 `httpx>=0.27`）。
- [x] 建立只读演示文件与 `read_file` 工具，限制到演示工作区（`workspaces/demo_readonly/`、`src/miniloop/tools/read_file.py`）。
- [x] 实现任务 → 模型调用 → 工具执行 → 结果回填 → 模型终止（`src/miniloop/loop.py:run_loop`、`src/miniloop/cli.py`）。
- [x] 保存真实请求响应、调用 ID、实际文件结果与退出原因，不包含密钥（`src/miniloop/trajectory.py:TrajectoryRecorder`）。

**验证命令与证据：**
- 运行命令：
  ```bash
  uv run python -m miniloop.cli run \
    --provider openai_compatible \
    --base-url http://127.0.0.1:3425/v1 \
    --model remote-magpie/auto/claude-sonnet-5-5 \
    --workspace ./workspaces/demo_readonly \
    --task "Please read README_DEMO.md and config_sample.ini in the workspace using read_file, and report the service codename, maintenance window, and listen_port." \
    --output /tmp/p0_01_real_run.jsonl
  ```
- 证据路径：[docs/status/p0-01-first-real-loop.md](status/p0-01-first-real-loop.md)（提交 `5749d66`）。

### P0-02 工具协议与错误处理

- [x] 工具通过注册接入；校验工具名、必需字段、类型及调用 ID（`src/miniloop/tools/registry.py:ToolRegistry`、`validate_tool_arguments`）。
- [x] 明确未知工具、错误参数、工具异常和协议错误的处理方式（`ToolExecutionError` 转为 `ToolResult(is_error=True)` 回填模型；缺失/重复 `call_id` 或非法 JSON 参数触发 `ProtocolViolationError` 并以 `exit_reason="protocol_error"` 终止）。
- [x] 能正确处理同一轮多个调用，首版顺序执行并逐个关联结果（`src/miniloop/loop.py`、协议场景 `S09`）。
- [x] 引入确定性模型替身，验证协议行为；替身与真实模型入口明确区分（`src/miniloop/adapter.py:FakeModelAdapter`、`src/miniloop/protocol_verify.py`）。

**验证命令与证据：**
- 运行命令：`uv run python -m miniloop.cli verify-protocol --check` 与 `uv run pytest tests/test_loop_and_protocol.py -q`
- 证据路径：`src/miniloop/protocol_verify.py`（场景 `S01`、`S02`、`S03`、`S08`、`S09`）及 [docs/status/evaluation-report.md](status/evaluation-report.md)（Track A 表与 `case_06_lru_eviction` 真实乐观锁失效恢复轨迹）。

### P0-03 预算、超时与退出

- [x] 实现模型调用上限、工具调用上限、总耗时预算与单次操作超时（`src/miniloop/budget.py:BudgetTracker`）。
- [x] 区分正常结束、预算耗尽、模型错误、协议错误、取消与运行时错误（`src/miniloop/types.py:ExitReason`）。
- [x] 所有重试计入预算；超限后不再启动新的工具或模型请求（`src/miniloop/loop.py` 跨轮与重试前剩余时间核查，提交 `706bd8f`）。
- [x] 使用确定性替身覆盖边界，真实调用仅验证适配器实际行为（`tests/test_budget_and_trajectory.py`、`src/miniloop/protocol_verify.py` 场景 `S04`、`S05`、`S06`）。

**验证命令与证据：**
- 运行命令：`uv run pytest tests/test_budget_and_trajectory.py -q` 与 `uv run python -m miniloop.cli verify-protocol --check`
- 证据路径：`src/miniloop/budget.py`、`src/miniloop/loop.py`、[docs/status/evaluation-report.md](status/evaluation-report.md)。

### P0-04 一个真实代码修复任务

- [x] 制作有稳定故障、参考修复和固定测试的小型 Python 案例（`cases/case_01_off_by_one`、`cases/case_02_none_guard` 等，`src/miniloop/cases.py`，提交 `0df1cd3`）。
- [x] 实现提出差异、确认和应用差异，只修改演示副本的允许源码（`src/miniloop/tools/patch.py:PatchStore`、`propose_patch`、`apply_patch`，提交 `e777619`）。
- [x] `run_tests` 使用固定入口，模型不能修改评分测试或执行任意命令（`src/miniloop/tools/run_tests.py:build_run_tests_tool`，执行前后核对测试文件 SHA-256）。
- [x] 真实模型完成读取、提出修改、确认后应用及测试验证；保留失败尝试（`src/miniloop/eval_runner.py`，真实模型在 `case_06_lru_eviction` 中触发 `patch_stale_file_changed` 后重新提议补丁并修复通过）。

**验证命令与证据：**
- 运行命令：
  ```bash
  uv run python -m miniloop.cli eval \
    --split dev \
    --provider openai_compatible \
    --base-url http://127.0.0.1:3425/v1 \
    --model remote-magpie/auto/claude-sonnet-5-5
  ```
- 证据路径：[docs/status/evaluation-report.md](status/evaluation-report.md)、`tests/test_tools_patch_and_tests.py`、`tests/test_eval_runner.py`。

## P1：可检查的轨迹与可信交付

### P1-01 JSONL 轨迹与只读回放

- [x] 固定事件字段和版本（Schema `v1.0`），覆盖模型、工具、错误、预算及终态（`src/miniloop/trajectory.py`）。
- [x] 回放只读文件，不执行工具或请求模型（`src/miniloop/trajectory.py:replay_trajectory`、`miniloop replay`）。
- [x] 显示调用 ID 关联，并明确提示损坏、截断及缺失事件（`TrajectoryCorruptedError`、协议场景 `S10`）。

**验证命令与证据：**
- 运行命令：`uv run python -m miniloop.cli replay runs/eval_all_claude_sonnet_5_5/case_06_lru_eviction/trajectory.jsonl`
- 证据路径：`src/miniloop/trajectory.py`、`tests/test_budget_and_trajectory.py`、协议场景 `S10`（断言回放期间 `model_calls == 0` 且 `tool_executions == 0`）。

### P1-02 分开记录协议验证与真实任务评测

- [x] 覆盖正常结束、错误参数、未知工具、工具超时、模型错误、预算耗尽、拒绝修改、调用关联错误等 10 类协议情形（`src/miniloop/protocol_verify.py` `S01`–`S10`）。
- [x] 准备 6 个独立修复案例（`dev` 2 例 + `test` 4 例），附 `cases/MANIFEST.json` SHA-256 冻结校验与人工参考补丁自检（`src/miniloop/cases.py`、`tests/test_cases_manifest.py`）。
- [x] 固定任务分组、模型、提示词、预算和独立验证器，保留首次完整结果（`src/miniloop/eval_runner.py`）。
- [x] 分别报告协议结果（Track A：10/10）与真实任务完成数（Track B：`dev` 2/2、`test` 4/4，总计 6/6）、耗时、Token 用量与工具错误归因。

**验证命令与证据：**
- 运行命令：
  ```bash
  uv run python -m miniloop.cli eval \
    --split all \
    --provider openai_compatible \
    --base-url http://127.0.0.1:3425/v1 \
    --model remote-magpie/auto/claude-sonnet-5-5 \
    --output-dir runs/eval_all_claude_sonnet_5_5 \
    --report-out docs/status/evaluation-report.md
  ```
- 证据路径：[docs/status/evaluation-report.md](status/evaluation-report.md)。

### P1-03 可复现交付与本人讲解

- [x] 补充实际启动命令、配置加载说明、演示案例和常见错误（[README.md](../README.md)）。
- [x] 提供一键离线/在线验收脚本 `./scripts/verify.sh` 并验证通过（`scripts/verify.sh`）。
- [x] 更新参考版本、复用代码归属和本人新增实现（[REFERENCES.md](../REFERENCES.md)）。
- [x] 能在报告中解释真实工具错误恢复轨迹（`case_06_lru_eviction` 中同轮双补丁引发的 `patch_stale_file_changed` 乐观锁拦截与重新提议），并通过 `ToolRegistry.register` 在不改主循环的情况下接入扩展工具。

**验证命令与证据：**
- 离线全套门禁：`./scripts/verify.sh --offline`
- 含真实模型冒烟验证：`./scripts/verify.sh`

## 后续范围与未实现边界

- 本项目定位于单模型适配器、单串行执行循环的教学与协议验证内核。
- 多智能体编排、上下文自动压缩、跨进程中断恢复（checkpoint resume）、容器级沙箱与外部大型榜单（如完整 SWE-bench）仍属于非目标与未实现范围。
