# 学习参考与代码归属

## 一手来源

| 项目 | 用途 | 公开许可信息 |
| --- | --- | --- |
| [shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) | 学习 Agent loop、结构化工具调用及权限相关机制；属于第三方教学项目 | [MIT](https://github.com/shareAI-lab/learn-claude-code/blob/main/LICENSE)，版权署名 shareAI Lab |
| [SWE-agent/mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) | 对照 Agent、模型、环境的职责以及线性执行轨迹；其 Bash 主导接口与本项目的结构化工具不同 | [MIT](https://github.com/SWE-agent/mini-swe-agent/blob/main/LICENSE.md)，版权署名 Kilian A. Lieret、Carlos E. Jimenez |
| [earendil-works/pi](https://github.com/earendil-works/pi) | 作为状态、工具和事件架构参考；主要使用 TypeScript，不作为 Python 逐行实现材料 | [MIT](https://github.com/earendil-works/pi/blob/main/LICENSE)，版权署名 Mario Zechner |

以上是公开仓库页面与许可证的核对结果。旧 Pi 地址 `badlogic/pi-mono` 在核对时重定向到上述仓库。“mini-pi”存在同名项目，本项目未假定某个同名仓库是指定来源。

## 本仓库实现与上游关系说明

- **零上游代码复制**：`src/miniloop/`、`tests/` 与 `cases/` 下的全部代码、测试用例和修复案例均使用 Python 3.12 标准库及 `httpx` 从零独立编写，未拷贝或改编上述仓库的任何源文件。
- **架构差异明确**：不同于 `mini-swe-agent` 以自由 Bash 命令为主的执行方式，MiniLoop 采用零黑盒 JSON Schema 参数前置校验与 4 个显式受限工具（`read_file`、`propose_patch`、`apply_patch`、`run_tests`），并在 `propose_patch` / `apply_patch` 之间引入基于目标文件 SHA-256 的两阶段乐观锁与审批回调。
- **许可证与成果边界**：本项目许可证尚未选定，不把上游 MIT 标识当成本项目许可声明；所有评测指标均来自本仓库冻结的 6 个小型 Python 修复案例与 10 类确定性协议测试，绝不引用上游测试数或榜单成绩。

## 概念对照与本人独立实现归属表

| 概念领域 | 概念对照来源 | 代码复用情况 | 本人独立实现模块与验证证据 |
| --- | --- | --- | --- |
| 串行 Agent 主循环与 `call_id` 配对 | `shareAI-lab/learn-claude-code`、`SWE-agent/mini-swe-agent` 的线性消息循环思路 | 零代码复用（完全独立编写） | `src/miniloop/loop.py`、`src/miniloop/adapter.py`；由 `tests/test_loop_and_protocol.py` 与 [P0-01 报告](docs/status/p0-01-first-real-loop.md) 验证 |
| 工具注册表、前置 Schema 校验与 `ToolError` / `ProtocolError` 二分 | `earendil-works/pi` 与 OpenAI Function Calling 的结构化工具边界思路 | 零代码复用（使用 Python 标准库实现校验器） | `src/miniloop/tools/registry.py`、`src/miniloop/protocol_verify.py`（场景 `S02`、`S03`、`S08`、`S09`） |
| 两阶段乐观锁补丁（`propose_patch` / `apply_patch`）与固定入口 `run_tests` | 受控修改审批与测试防篡改设计 | 零代码复用（原创 SHA-256 乐观锁与测试文件哈希前置/后置核验） | `src/miniloop/tools/patch.py`、`src/miniloop/tools/run_tests.py`；由 `tests/test_tools_patch_and_tests.py` 及 `case_06_lru_eviction` 真实轨迹中的 `patch_stale_file_changed` 自愈过程验证 |
| 线性脱敏 JSONL 轨迹（Schema v1.0）与纯只读回放 | `SWE-agent/mini-swe-agent` 轨迹落盘思路 | 零代码复用（独立定义 Schema v1.0、递归脱敏与零执行回放校验） | `src/miniloop/trajectory.py`；由 `tests/test_budget_and_trajectory.py` 与协议场景 `S10`（回放期间模型/工具调用计数严格为 `0`）验证 |
| 冻结修复案例集（2 `dev` + 4 `test`）与独立工作区评分器 | 软件修复基准的“循环退出 vs. 独立测试验证分离”原则 | 零代码复用（6 个自包含 Python 案例及 `MANIFEST.json` 均为独立编写） | `cases/`、`src/miniloop/cases.py`、`src/miniloop/eval_runner.py`；由 `tests/test_cases_manifest.py`、`tests/test_eval_runner.py` 与 [双轨评测报告](docs/status/evaluation-report.md) 验证 |
