# MiniLoop MVP 与路线图：规格与实施计划

- 项目名：**MiniLoop**；Python 包名与命令行入口为 `miniloop`。
- 状态：v1（2026-11-06）。
- 仓库路径：`/data/miniloop/repo`（软链 `/home/carryz/MiniLoop`）；多工作树目录：`/data/miniloop/wt/`；监督与任务状态目录：`/data/miniloop/state/`。
- 基线提交：`c7fb937`（`chore: initialize MiniLoop scaffold and implementation plans`）。

本文是唯一的实施依据，对标 `Lab-Agent` 的研发与监督工程规范。任务按 **PR 切片 + 并行波次（Wave）** 排列，每个 PR 写明所属波次、分支、工作树、`hot_resources`（串行热点资源）、`allowed_paths`（允许修改的文件白名单）、接口契约、测试门禁与验收标准。

---

## 0. 使用说明与约定

### 0.1 术语

| 术语 | 含义 |
| --- | --- |
| 模型适配器 (`ModelAdapter`) | 将内部消息历史与工具定义转换为模型接口请求，并将响应解析为文本、`ToolCall` 列表与 `TokenUsage` 的边界层 |
| 真实模型模式 (`openai_compatible`) | 经本机 Magpie 网关（`http://127.0.0.1:3425/v1`）调用真实大模型（如 `claude-sonnet-5-5`、`gemini-3.8-flash`、`gemini-4-argon`），绝不使用预设替身响应 |
| 确定性替身模式 (`fake`) | 使用 `FakeModelAdapter` 按预设脚本返回工具调用、文本或受控异常，零网络依赖，用于协议验证与 CI |
| 工具级错误 (`ToolError`) | 未知工具名、参数缺失/类型错误、路径越界、文件不存在、补丁不匹配等可在拦截副作用后封装为 `ToolResult(is_error=True)` 回填给模型继续决策的错误 |
| 协议级错误 (`ProtocolError`) | 模型返回的 `tool_call` 缺失 `id`、同轮 `id` 重复、JSON 参数无法反序列化或响应结构损坏，必须立即终止循环并记为 `exit_reason="protocol_error"` |
| 乐观锁补丁 (`PatchProposal`) | `propose_patch` 生成的待确认差异记录，绑定目标文件当时的 `sha256`；`apply_patch` 执行前若文件 `sha256` 已变化，旧确认立即失效 |
| 终态 (`ExitReason`) | 循环退出的明确枚举：`completed` / `step_limit` / `time_limit` / `model_error` / `protocol_error` / `cancelled` / `runtime_error`。`completed` **不等于**代码修复成功 |
| 轨迹回放 (`replay`) | 纯只读解析已落盘的 JSONL 轨迹文件，展示事件序列与 `call_id` 配对关系；**绝不请求模型、绝不实例化或执行任何工具** |

### 0.2 给实施 Agent 与监督者的全局规则

1. **先读后改，严守 `allowed_paths`**：每个 PR 只允许修改其卡片头部声明的 `allowed_paths`。监督者复核时必跑 `git diff --name-only origin/main...HEAD` 校验零越界。
2. **每个 PR 必须通过全套本地门禁**才能 push / 开 PR：
   ```bash
   uv run ruff check src tests
   uv run mypy src tests
   uv run pytest -q
   uv run python -m miniloop.cli verify-protocol --check   # PR-05 起必跑
   ```
3. **不得手改评测数字或伪造真实模型轨迹**：`docs/status/` 下的报告与 `runs/` 下的轨迹只能由真实命令生成。确定性替身（`fake`）跑出的结果严禁标为真实模型通过；真实模式缺少密钥或网关不可达时必须直接报错退出，**严禁静默回退到 `fake`**。
4. **不得提交任何真实密钥**：
   - Magpie 网关密钥优先从环境变量 `MINILOOP_MODEL_API_KEY` 或密钥文件 `MINILOOP_MODEL_API_KEY_FILE`（默认 `~/.config/miniloop/magpie.key`，若不存在可回退读取 `~/.config/lab-agent/magpie.key`）加载，使用 `SecretStr` 或不可打印封装。
   - 任何密钥、请求头 `Authorization`、无关环境变量不得写入 `.env`、JSONL 轨迹、日志或异常信息。
5. **本机网络地址规则**：代码默认值只允许使用 `127.0.0.1`（如 `http://127.0.0.1:3425/v1`），不得把局域网或 Tailscale IP 写进代码默认值。
6. **代码风格与依赖克制**：
   - Python `>=3.12`，全部模块启用 `from __future__ import annotations`，通过 `mypy --strict` 与 `ruff check`。
   - 运行时仅引入 `httpx`（用于直接、透明地构造与检查 HTTP JSON 载荷，不用黑盒 SDK 隐藏工具调用报文）；工具参数校验与 JSONL 回放均使用 Python 标准库实现，保持教学内核 100% 透明可讲清。
7. **GitHub PR + CI + Codex 审查闭环**：
   - 每个 PR 推送后等待 GitHub Actions CI 全绿 + `chatgpt-codex-connector[bot]` 针对最新 `HEAD_SHA` 审查 `clean`（0 未解决线程），再按串行合并列车 `gh pr merge --squash --delete-branch --match-head-commit "$HEAD_SHA"` 合入 `main`。

---

## 1. 现状评估与差距清单

### 1.1 组件现状（基线 `c7fb937`）

| 领域 | 现状 | 证据 | 判定 |
| --- | --- | --- | --- |
| 包结构与构建 | `pyproject.toml` 仅有 setuptools 基础配置，`src/miniloop/__init__.py` 仅含 `__version__ = "0.1.0.dev0"` | `pyproject.toml`、`src/miniloop/__init__.py` | ⛔ 仅空骨架 |
| 设计与验收文档 | 架构边界、P0/P1 清单、评测协议、开源参考说明齐全且口径诚实 | `README.md`、`docs/*.md`、`REFERENCES.md` | ✅ 设计清晰 |
| 模型适配器与密钥加载 | `.env.example` 仅列变量名，无配置解析、无密钥文件读取、无 HTTP 适配器或替身 | `.env.example` | ⛔ 未实现 |
| 工具系统 | 无注册表、无参数校验、无 `read_file` / `propose_patch` / `apply_patch` / `run_tests` | `src/miniloop/` | ⛔ 未实现 |
| 主循环与预算控制 | 无执行循环、无调用 ID 配对、无步数/时间预算与超时退出 | `src/miniloop/` | ⛔ 未实现 |
| 轨迹记录与回放 | 无 JSONL 记录器、无脱敏过滤器、无只读回放器 | `src/miniloop/` | ⛔ 未实现 |
| 协议验证与修复评测 | 无 `tests/`、无确定性协议套件、无修复案例 `cases/`、无 CI 工作流 | 仓库根目录 | ⛔ 未实现 |

### 1.2 关键差距编号（G1–G8）

| # | 差距 | 对应路线图 | 落地 PR |
| --- | --- | --- | --- |
| G1 | 缺少工程门禁（`ruff`、`mypy --strict`、`pytest`、`gitleaks` CI）、安全密钥加载器与统一核心类型定义 | P0-01 | PR-01 |
| G2 | 缺少真实模型适配器（OpenAI 兼容 `/v1/chat/completions`）与确定性模型替身（`FakeModelAdapter`） | P0-01、P0-02 | PR-02 |
| G3 | 缺少通用工具注册表、前置参数 Schema 校验器及受根目录限制的只读 `read_file` 工具 | P0-01、P0-02 | PR-03 |
| G4 | 缺少步数/工具次数/墙钟预算控制器、脱敏 JSONL 轨迹记录器与纯只读回放器 | P0-03、P1-01 | PR-04 |
| G5 | 缺少串行工具调用主循环、`ToolError` vs `ProtocolError` 分层、CLI 入口与 10 类确定性协议验证套件 | P0-01 ~ P0-03、P1-01 | PR-05 |
| G6 | 缺少受限修改工具链（`propose_patch` 乐观锁、`ApprovalPolicy`、`apply_patch`）与固定测试入口 `run_tests` | P0-04 | PR-06 |
| G7 | 缺少带 `MANIFEST.json` SHA-256 冻结与人工参考补丁验证的 6 个独立 Python 修复案例（`dev` + `test`） | P0-04、P1-02 | PR-07 |
| G8 | 缺少分离协议验证与真实模型评测的 `eval_runner`、真实模型评测报告、一键验收脚本与归属文档更新 | P0-04、P1-02、P1-03 | PR-08 |

---

## 2. 目标与范围

### 2.1 MVP 与 P1 完整交付定义

完成后在干净环境或克隆仓库中可执行：

```bash
# 1. 离线门禁与 10 类确定性协议验证（零网络、零密钥，CI 每次必跑）
./scripts/verify.sh --offline

# 2. 真实模型模式（走本机 Magpie 网关 http://127.0.0.1:3425/v1）
export MINILOOP_MODEL_PROVIDER=openai_compatible
export MINILOOP_MODEL_BASE_URL=http://127.0.0.1:3425/v1
export MINILOOP_MODEL_NAME=claude-sonnet-5-5
./scripts/verify.sh
```

满足：
1. **P0-01 真实只读闭环**：模型对 `workspaces/demo_readonly/` 发起真实 `read_file` 调用，程序校验路径并读取文件，按 `call_id` 回填结果，模型给出最终回答并以 `completed` 退出，落盘脱敏轨迹。
2. **P0-02 / P0-03 / P1-01 确定性协议验证（10/10 全绿）**：`uv run python -m miniloop.cli verify-protocol --check` 覆盖全部 10 类确定性协议情形，断言实际副作用与计数器（回放时模型与工具调用计数严格为 0）。
3. **P0-04 / P1-02 代码修复案例与双轨评测**：6 个独立 Python 故障案例（`dev` 2 例 + `test` 4 例）全部通过人工参考补丁自检；真实模型完成 `dev` 修复验证与 `test` 首次冻结评测，生成可追溯到逐例 JSONL 轨迹的 `docs/status/evaluation-report.md`。

### 2.2 完成定义（DoD）

- [ ] PR-01 ~ PR-08 全部通过本地门禁、GitHub Actions CI 与 Codex Review（0 遗留问题）并合入 `main`。
- [ ] `uv run python -m miniloop.cli verify-protocol --check` 10/10 场景全绿。
- [ ] `docs/status/p0-01-first-real-loop.md` 与 `docs/status/evaluation-report.md` 均由真实运行生成，包含运行提交、配置、模型名、逐例结果与失败归因，不含任何密钥。
- [ ] `docs/roadmap.md`、`README.md`、`REFERENCES.md` 同步更新真实状态、提交哈希与验证命令。

---

## 3. 架构决策（D1–D8）

| # | 决策 | 理由 | 落地 PR |
| --- | --- | --- | --- |
| D1 | **单模型适配器采用 `httpx` 直接对接 OpenAI 兼容 `/v1/chat/completions`（指向本机 Magpie 网关）** | 相比封装厚重的官方 SDK，直接用 `httpx` 发送和解析 `tools` / `tool_calls` / `role="tool"` JSON 载荷代码不到 150 行，能完全看清协议细节、精确控制单次超时与脱敏记录，且无缝支持 Magpie 上的所有模型 | PR-01、PR-02 |
| D2 | **密钥双源解析（环境变量 + 密钥文件）与全链路脱敏** | 支持 `MINILOOP_MODEL_API_KEY` 与 `MINILOOP_MODEL_API_KEY_FILE`（默认依次探测 `~/.config/miniloop/magpie.key`、`~/.config/lab-agent/magpie.key`）；`SecretValue` 屏蔽 `__repr__`/`__str__`；轨迹写入前统一经 `sanitize_payload` 过滤敏感键与已知密钥明文 | PR-01、PR-04 |
| D3 | **零黑盒工具参数 Schema 校验器** | 每个 `ToolDefinition` 声明 `parameters_schema`（JSON Schema 子集：`type="object"`、`properties`、`required`、`additionalProperties=False`）；注册表在调用工具函数前校验必填项、类型（`str`/`int`/`float`/`bool`）与未知字段，校验失败直接返回 `ToolResult(is_error=True, error_code="invalid_arguments")`，绝不触达副作用 | PR-03 |
| D4 | **错误严格二分：`ToolError`（回填模型） vs `ProtocolError`（终止循环）** | 未知工具、参数不合法、文件不存在、路径越界、补丁被拒或 `sha256` 冲突均属于 `ToolError`，组装为关联原 `call_id` 的错误结果回填给模型；而 `call_id` 缺失、同轮 `call_id` 重复、模型返回的 arguments 不是合法 JSON 对象则属于 `ProtocolError`，立即以 `exit_reason="protocol_error"` 结束 | PR-03、PR-05 |
| D5 | **统一剩余时间预算（Deadline 驱动）** | 循环启动时计算 `deadline = time.monotonic() + max_wall_time_sec`；每次请求模型或执行工具前检查步数/工具次数上限与 `remaining = deadline - time.monotonic()`，实际单次超时取 `min(per_op_timeout, remaining)`，杜绝超限后仍发起新调用 | PR-04、PR-05 |
| D6 | **两阶段乐观锁代码修改（`propose_patch` → `ApprovalPolicy` → `apply_patch`）** | `propose_patch(path, old_str, new_str)` 仅校验路径在允许修改白名单内（硬拦截 `tests/`）且 `old_str` 唯一存在，记录文件当前 `sha256` 并生成 `patch_id` 与 unified diff；`apply_patch(patch_id)` 经 `ApprovalPolicy` 批准且核对文件当前 `sha256` 未变后才原子覆写文件；文件内容一旦改变，旧 `patch_id` 立即失效 | PR-06 |
| D7 | **固定测试入口 `run_tests` 与独立评分器防篡改** | `run_tests` 零参数，不接受模型传入任何命令或路径，仅在隔离副本中执行案例绑定的 pytest 命令；案例初始化时记录 `tests/` 下所有文件的 `sha256`，测试执行前后与循环结束评分时均核对测试文件哈希，若被篡改直接判修复失败 | PR-06、PR-07、PR-08 |
| D8 | **JSONL 轨迹 Schema v1.0 与零副作用只读回放** | 每条记录必含 `schema_version="1.0"`、`run_id`、`sequence`（从 1 连续递增）、`timestamp`、`event_type`、`payload`；`replay_trajectory` 仅解析文件并校验序列连续性、`call_id` 配对闭合与 `run_end` 存在性，不导入模型客户端、不执行任何工具 | PR-04、PR-05 |

---

## 4. 并行波次（Waves）与按 PR 详细规格（PR-01 ~ PR-08）

### Wave 0 — 工程基线、核心类型契约与 CI 门禁（单 PR 串行）

#### PR-01（T0.1）：工程工具链、密钥读取、核心类型契约与 GitHub Actions CI
- **Branch**: `feat/pr-01-core-types-config-ci`
- **Worktree**: `/data/miniloop/wt/pr-01`
- **`hot_resources`**: `["pyproject.toml", "types.py", "config.py"]`
- **`allowed_paths`**:
  - `pyproject.toml`
  - `.env.example`
  - `.gitleaks.toml`
  - `.github/workflows/ci.yml`
  - `src/miniloop/__init__.py`
  - `src/miniloop/types.py`
  - `src/miniloop/secrets.py`
  - `src/miniloop/config.py`
  - `tests/__init__.py`
  - `tests/test_config_and_secrets.py`
- **Scope**:
  1. **`pyproject.toml`**：添加运行时依赖 `httpx>=0.27`；添加可选开发依赖 `[project.optional-dependencies] dev = ["pytest>=8.0", "ruff>=0.6", "mypy>=1.11"]`；配置 `[project.scripts] miniloop = "miniloop.cli:main"`；配置 `[tool.ruff]` 与 `[tool.mypy]`（`strict = true`）。
  2. **`src/miniloop/types.py`（冻结全局核心数据结构）**：
     - `ExitReason(StrEnum)`：`COMPLETED = "completed"`、`STEP_LIMIT = "step_limit"`、`TIME_LIMIT = "time_limit"`、`MODEL_ERROR = "model_error"`、`PROTOCOL_ERROR = "protocol_error"`、`CANCELLED = "cancelled"`、`RUNTIME_ERROR = "runtime_error"`。
     - `ToolCall( dataclass(frozen=True) )`：`id: str`、`name: str`、`arguments: dict[str, Any]`、`raw_arguments: str`。
     - `ToolResult( dataclass(frozen=True) )`：`call_id: str`、`tool_name: str`、`content: str`、`is_error: bool = False`、`error_code: str | None = None`、`duration_ms: float = 0.0`。
     - `TokenUsage( dataclass(frozen=True) )`：`prompt_tokens: int | None = None`、`completion_tokens: int | None = None`、`total_tokens: int | None = None`。
     - `Message( dataclass(frozen=True) )`：`role: Literal["system", "user", "assistant", "tool"]`、`content: str | None = None`、`tool_calls: tuple[ToolCall, ...] = ()`、`tool_call_id: str | None = None`、`name: str | None = None`。
     - `ModelTurnResponse( dataclass(frozen=True) )`：`content: str | None`、`tool_calls: tuple[ToolCall, ...]`、`usage: TokenUsage`、`model_id: str`、`raw_response: dict[str, Any]`、`duration_ms: float`。
     - `BudgetConfig( dataclass(frozen=True) )`：`max_model_calls: int = 10`、`max_tool_calls: int = 15`、`max_wall_time_sec: float = 120.0`、`per_request_timeout_sec: float = 30.0`、`per_tool_timeout_sec: float = 15.0`、`max_model_retries: int = 2`。
     - `TrajectoryEvent( dataclass(frozen=True) )`：`schema_version: str`、`run_id: str`、`sequence: int`、`timestamp: str`、`event_type: str`、`payload: dict[str, Any]`。
     - `RunResult( dataclass(frozen=True) )`：`run_id: str`、`exit_reason: ExitReason`、`final_answer: str | None`、`model_calls: int`、`tool_calls: int`、`wall_time_sec: float`、`trajectory_path: Path | None`、`error_detail: str | None = None`。
  3. **`src/miniloop/secrets.py` 与 `src/miniloop/config.py`**：
     - `SecretValue`：封装密钥字符串，`__repr__` 与 `__str__` 恒返回 `"SecretValue('***')"`，通过 `.get_secret_value()` 获取明文。
     - `read_secret(env_var: str, file_env_var: str, default_files: Sequence[Path] = ()) -> SecretValue | None`：按环境变量 → 显式密钥文件 → 默认密钥文件路径依次解析；空文件抛 `ValueError` 且异常信息绝不含文件内容。
     - `ModelConfig`：从环境变量加载 `MINILOOP_MODEL_PROVIDER`（`"fake"` | `"openai_compatible"`）、`MINILOOP_MODEL_BASE_URL`（默认 `"http://127.0.0.1:3425/v1"`）、`MINILOOP_MODEL_NAME`、`MINILOOP_MODEL_API_KEY` / `MINILOOP_MODEL_API_KEY_FILE`。当 `provider == "openai_compatible"` 时强制校验 `model_name` 与 `api_key` 非空，缺失直接抛 `ConfigError`（绝不回退到 `fake`）。
  4. **`.github/workflows/ci.yml` 与 `.gitleaks.toml`**：
     - 遵循 CI 规范：配置 `concurrency`（`main` 按 `run_id` 隔离不丢运行，PR 自动取消旧运行）、`actions/checkout`（`persist-credentials: false`）、`astral-sh/setup-uv`（开启内置缓存），运行 `gitleaks git --log-opts=--all`、`uv run ruff check src tests`、`uv run mypy src tests`、`uv run pytest -q`。
- **验收标准**：`uv run ruff check src tests && uv run mypy src tests && uv run pytest -q` 全绿；哨兵密钥测试证明 `repr(config)`、日志和异常中不出现密钥明文。

---

### Wave 1 — 三大核心模块并行开发（3 个独立 Worktree 零文件重叠并行）

`PR-02`（模型适配器）、`PR-03`（工具注册表与 `read_file`）、`PR-04`（预算器与 JSONL 轨迹/回放）均只依赖 `PR-01` 的 `types.py` / `config.py`，彼此 `allowed_paths` 完全正交，可在 3 个 git worktree 中同时开发。

#### PR-02（T1.1）：OpenAI 兼容模型适配器与确定性替身 `FakeModelAdapter`
- **Branch**: `feat/pr-02-model-adapters`
- **Worktree**: `/data/miniloop/wt/pr-02`
- **`hot_resources`**: `[]`
- **`allowed_paths`**:
  - `src/miniloop/adapter.py`
  - `tests/test_adapter.py`
- **Scope**:
  1. 定义 `ModelAdapter(Protocol)`：`provider: str`、`model_name: str`、`generate(messages: Sequence[Message], tools: Sequence[dict[str, Any]], *, timeout_sec: float) -> ModelTurnResponse`、`close() -> None`。
  2. 定义异常：`ModelAdapterError`（网络超时、HTTP 4xx/5xx 重试耗尽）与 `ProtocolViolationError`（响应缺少 `choices`、`tool_call` 缺少非空 `id` 或 `function.name`、同一轮 `tool_calls` 内出现重复 `id`、`function.arguments` 不是合法 JSON `dict`）。
  3. 实现 `OpenAICompatibleAdapter`：
     - 使用 `httpx.Client` 发送 `POST {base_url}/chat/completions`；
     - 严格映射 `Message`（含 `role="assistant"` 的 `tool_calls` 与 `role="tool"` 的 `tool_call_id`）及 `tools` 列表；
     - 解析返回的 `tool_calls`、验证每个 `call.id` 非空且在当轮唯一、反序列化 `arguments` 为 `dict[str, Any]`（若非 JSON 对象抛 `ProtocolViolationError`）；
     - 提取 `usage`（`prompt_tokens` / `completion_tokens` / `total_tokens`，未返回则置 `None`）；
     - 仅对连接错误、超时、HTTP 429/5xx 按调用方给定的尝试上限重试（每次尝试均通过回调或返回值向预算器报告尝试次数）。
  4. 实现 `FakeModelAdapter`：
     - 接收预设动作序列（返回 `ModelTurnResponse`、抛出 `ModelAdapterError`、抛出 `ProtocolViolationError`、或者延迟 `sleep_sec` 模拟慢响应）；
     - 暴露 `call_count: int` 与 `recorded_requests: list[tuple[list[Message], list[dict[str, Any]]]]` 供协议测试精确断言。
- **验收标准**：`tests/test_adapter.py` 使用 `httpx.MockTransport` 测试正常文本回复、单/多 `tool_calls` 解析、`role="tool"` 回填序列化、HTTP 500 重试、缺失/重复 `call_id` 触发 `ProtocolViolationError`、非法 JSON arguments 触发 `ProtocolViolationError`。

#### PR-03（T1.2）：工具注册表、零黑盒参数 Schema 校验与只读 `read_file` 工具
- **Branch**: `feat/pr-03-tools-and-read-file`
- **Worktree**: `/data/miniloop/wt/pr-03`
- **`hot_resources`**: `[]`
- **`allowed_paths`**:
  - `src/miniloop/tools/__init__.py`
  - `src/miniloop/tools/registry.py`
  - `src/miniloop/tools/read_file.py`
  - `workspaces/demo_readonly/README_DEMO.md`
  - `workspaces/demo_readonly/config_sample.ini`
  - `tests/test_tools_read_file.py`
- **Scope**:
  1. **`src/miniloop/tools/registry.py`**：
     - `ToolDefinition`：`name: str`、`description: str`、`parameters_schema: dict[str, Any]`、`handler: Callable[[dict[str, Any], float], str]`；提供 `.to_openai_tool() -> dict[str, Any]`。
     - `validate_tool_arguments(schema: dict[str, Any], args: dict[str, Any]) -> str | None`：校验必填字段（`required`）、字段类型（`"string"`→`str`，`"integer"`→`int` 且排除 `bool`，`"number"`→`int|float` 且排除 `bool`，`"boolean"`→`bool`）以及禁止未声明字段（`additionalProperties: false`）。
     - `ToolRegistry`：`register(tool)`、`to_openai_tools()`、`execute(call: ToolCall, *, timeout_sec: float) -> ToolResult`。
       - 若 `call.name` 未注册 → 返回 `ToolResult(call_id=call.id, tool_name=call.name, content=..., is_error=True, error_code="unknown_tool")`（副作用执行次数 = 0）；
       - 若参数校验不通过 → 返回 `ToolResult(call_id=call.id, tool_name=call.name, content=..., is_error=True, error_code="invalid_arguments")`（副作用执行次数 = 0）；
       - 支持单次工具执行超时控制（超时返回 `is_error=True, error_code="tool_timeout"`）；
       - 工具内部抛出的 `ToolExecutionError` 转为 `ToolResult(is_error=True, error_code=err.code)`。
  2. **`src/miniloop/tools/read_file.py`**：
     - `build_read_file_tool(workspace_root: Path, *, max_file_bytes: int = 65536, max_output_chars: int = 16000) -> ToolDefinition`。
     - 路径安全检查：拒绝空路径，解析 `(workspace_root / rel_path).resolve()`，使用 `resolved.is_relative_to(workspace_root.resolve())` 严格拦截 `../` 穿越、绝对路径与指向工作区外的软链接（违者抛 `ToolExecutionError("path_outside_workspace", ...)`）；拦截目录、不存在的文件、超过 `max_file_bytes` 的文件与非 UTF-8 二进制文件。
  3. **`workspaces/demo_readonly/`**：创建两个内容固定的只读演示文件（含特定代号、端口号与配置项，供 P0-01 真实模型只读任务验证）。
- **验收标准**：`tests/test_tools_read_file.py` 覆盖正常读取、未知工具、缺参/错类型（含 `bool` 冒充 `int` 拦截）、`../` 路径穿越、指向外部文件的 symlink 拦截、超长文件拦截、工具执行超时。

#### PR-04（T1.3）：预算/超时控制器、JSONL 脱敏轨迹记录器与纯只读回放器
- **Branch**: `feat/pr-04-budget-and-trajectory`
- **Worktree**: `/data/miniloop/wt/pr-04`
- **`hot_resources`**: `[]`
- **`allowed_paths`**:
  - `src/miniloop/budget.py`
  - `src/miniloop/trajectory.py`
  - `tests/test_budget_and_trajectory.py`
- **Scope**:
  1. **`src/miniloop/budget.py`**：
     - `BudgetTracker`：初始化记录 `start_monotonic = time.monotonic()` 与 `deadline = start_monotonic + config.max_wall_time_sec`。
     - `check_before_model_call() -> ExitReason | None`：若剩余时间 `<= 0` 返回 `ExitReason.TIME_LIMIT`；若 `model_calls >= config.max_model_calls` 返回 `ExitReason.STEP_LIMIT`。
     - `check_before_tool_call() -> ExitReason | None`：若剩余时间 `<= 0` 返回 `ExitReason.TIME_LIMIT`；若 `tool_calls >= config.max_tool_calls` 返回 `ExitReason.STEP_LIMIT`。
     - `next_model_timeout() -> float` 与 `next_tool_timeout() -> float`：返回 `min(per_op_timeout, max(0.001, remaining_sec))`。
     - `record_model_attempt()`（含重试均计入 `model_calls`）与 `record_tool_call()`。
  2. **`src/miniloop/trajectory.py`**：
     - `SCHEMA_VERSION = "1.0"`。
     - `sanitize_payload(data: Any, *, extra_secrets: Sequence[str] = ()) -> Any`：递归遍历 `dict`/`list`/`str`，将键名匹配 `api_key`、`authorization`、`token`、`secret`、`password`（忽略大小写）的值替换为 `"[REDACTED]"`，并将字符串中出现的任何非空 `extra_secrets` 明文或 `Bearer <token>` 替换为 `"[REDACTED]"`。
     - `TrajectoryRecorder`：每次 `record(event_type, payload)` 自动分配从 `1` 严格递增的 `sequence`、UTC ISO-8601 `timestamp`，经 `sanitize_payload` 脱敏后立即以单行 JSON 追加写入 `jsonl_path` 并 `flush()`。
     - `replay_trajectory(jsonl_path: Path) -> ReplayReport`：
       - **纯只读解析**：绝不导入 `adapter` 或 `tools` 执行器；
       - 逐行校验：非空文件、每行合法 JSON、`schema_version == "1.0"`、`run_id` 一致、`sequence == expected_seq`（从 1 连续无跳号）、首事件为 `run_start`、末事件为 `run_end`（若缺失末事件则报 `TrajectoryCorruptedError("truncated_trajectory: missing run_end")`）；
       - 校验 `tool_call` 与 `tool_result` 的 `call_id` 配对关系，生成可读摘要文本 `format_replay_summary(report)`。
- **验收标准**：`tests/test_budget_and_trajectory.py` 验证步数上限、工具上限、总时间耗尽、重试计入预算、敏感字段与哨兵密钥脱敏、正常轨迹回放，以及损坏 JSON / 截断缺 `run_end` / `sequence` 跳号 / `call_id` 不匹配的明确报错。

---

### Wave 2 — 主执行循环汇合、10 类协议验证与 P0-01 真实模型首跑（单 PR 汇合）

#### PR-05（T1.4）：串行工具执行循环 `run_loop`、CLI 入口、10 类确定性协议验证与 P0-01 真实模型首跑
- **Dependencies**: `PR-02`, `PR-03`, `PR-04`
- **Branch**: `feat/pr-05-loop-cli-protocol-verify`
- **Worktree**: `/data/miniloop/wt/pr-05`
- **`hot_resources`**: `["loop.py", "cli.py"]`
- **`allowed_paths`**:
  - `src/miniloop/loop.py`
  - `src/miniloop/protocol_verify.py`
  - `src/miniloop/cli.py`
  - `tests/test_loop_and_protocol.py`
  - `docs/status/p0-01-first-real-loop.md`
- **Scope**:
  1. **`src/miniloop/loop.py: run_loop(...) -> RunResult`**：
     - 串联 `BudgetTracker`、`ModelAdapter`、`ToolRegistry`、`TrajectoryRecorder`；
     - 写入 `run_start` 事件 → 循环检查 `check_before_model_call()` → 记录 `model_request` → 调用 `adapter.generate(...)`（捕获 `ModelAdapterError` 记 `exit_reason=MODEL_ERROR`，捕获 `ProtocolViolationError` 记 `exit_reason=PROTOCOL_ERROR`）；
     - 若模型未返回 `tool_calls`：将最终回答写入 `run_end(exit_reason="completed")` 并返回 `RunResult`；
     - 若模型返回 1 个或多个 `tool_calls`：保持模型返回顺序串行遍历，每个调用前执行 `check_before_tool_call()`（超限立即写入 `run_end` 并终止，不再执行后续工具），记录 `tool_call` 事件 → 执行 `registry.execute(call, timeout_sec=...)` → 记录 `tool_result` 事件 → 追加 `Message(role="tool", tool_call_id=call.id, name=call.name, content=result.content)` 到消息历史，进入下一轮模型请求。
  2. **`src/miniloop/protocol_verify.py`（落地 `docs/evaluation.md` 全部 10 类确定性情形）**：
     - S01：正常单工具调用后完成（`completed`）；
     - S02：参数缺失或类型错误，断言工具副作用计数器为 `0`，错误回填后模型修正并完成；
     - S03：未知工具请求，返回明确 `unknown_tool` 错误并被妥善处理；
     - S04：单次工具超时，触发 `tool_timeout` 或 `time_limit` 停止；
     - S05：模型接口瞬态错误，有界重试计入 `model_calls`，耗尽后以 `model_error` 退出；
     - S06：无限请求工具的模型替身，精确在 `step_limit` 处停止，不产生超额调用；
     - S07：修改提议被拒绝（或只读保护拦截），目标工作区文件内容与哈希保持 100% 不变；
     - S08：调用 ID 缺失或同轮重复，立即以 `protocol_error` 终止，零工具执行；
     - S09：同一轮返回多个工具调用 `[call_1, call_2]`，按序执行且各自结果与 `call_1.id`、`call_2.id` 精确配对；
     - S10：对已生成的 JSONL 轨迹执行只读回放，断言 `FakeModelAdapter.call_count == 0` 且工具执行计数 `== 0`。
  3. **`src/miniloop/cli.py`**：
     - `miniloop verify-protocol [--check]`
     - `miniloop run --task "..." --workspace <dir> [--provider fake|openai_compatible] [--output <jsonl>]`
     - `miniloop replay <jsonl_path>`
  4. **P0-01 真实模型首跑验收（`docs/status/p0-01-first-real-loop.md`）**：
     - 通过本机 Magpie 网关运行 `miniloop run` 读取 `workspaces/demo_readonly/`，验证真实模型发起 `read_file`、程序读取文件并回填 `call_id`、模型输出准确答案并正常退出，保存脱敏记录摘要。
- **验收标准**：`uv run python -m miniloop.cli verify-protocol --check` 10/10 全绿；真实模型跑通 P0-01 并产出 `docs/status/p0-01-first-real-loop.md`。

---

### Wave 3 — 代码修复工具链与冻结修复案例集（2 个独立 Worktree 并行）

`PR-06`（`propose_patch` / `apply_patch` / `run_tests` 工具实现）与 `PR-07`（6 个冻结修复案例 `cases/` 与 `MANIFEST.json`）文件触点完全隔离，可双路并行开发。

#### PR-06（T2.1）：受限补丁工具 `propose_patch` / `apply_patch`、审批策略与固定测试入口 `run_tests`
- **Dependencies**: `PR-05`
- **Branch**: `feat/pr-06-patch-and-test-tools`
- **Worktree**: `/data/miniloop/wt/pr-06`
- **`hot_resources`**: `["tools/__init__.py"]`
- **`allowed_paths`**:
  - `src/miniloop/tools/__init__.py`
  - `src/miniloop/tools/patch.py`
  - `src/miniloop/tools/run_tests.py`
  - `tests/test_tools_patch_and_tests.py`
- **Scope**:
  1. **`src/miniloop/tools/patch.py`**：
     - `PatchProposal`：`patch_id: str`、`rel_path: str`、`old_str: str`、`new_str: str`、`base_sha256: str`、`unified_diff: str`、`status: Literal["pending", "approved", "rejected", "applied", "invalidated"]`。
     - `ApprovalCallback = Callable[[PatchProposal], bool]`：提供内置策略 `approve_all_allowed()`、`reject_all()` 与交互式终端确认。
     - `propose_patch(path: str, old_str: str, new_str: str)`：
       - 校验 `path` 在工作区内且属于案例允许修改的 `editable_files` 白名单（若指向 `tests/` 或非白名单文件，返回 `ToolExecutionError("patch_path_forbidden", ...)`）；
       - 校验 `old_str != new_str` 且 `old_str` 在当前文件中**恰好出现 1 次**（0 次报 `old_str_not_found`，多次报 `old_str_not_unique`）；
       - 计算文件当前 `sha256`，生成 `patch_id`（如 `patch_0001`）与标准 `unified_diff`，触发 `ApprovalCallback` 记录审批状态，**不修改磁盘文件**。
     - `apply_patch(patch_id: str)`：
       - 校验 `patch_id` 存在；若状态为 `rejected` 则返回 `ToolExecutionError("patch_rejected", ...)` 且文件保持不变；
       - 重新计算磁盘目标文件当前 `sha256`：若与 `proposal.base_sha256` 不一致，将该 proposal 标记为 `invalidated` 并返回 `ToolExecutionError("patch_stale_file_changed", ...)`；
       - 通过后原子写入新内容，更新状态为 `applied`。
  2. **`src/miniloop/tools/run_tests.py`**：
     - `build_run_tests_tool(workspace_root: Path, *, test_command: Sequence[str], protected_hashes: Mapping[str, str]) -> ToolDefinition`。
     - 参数 Schema 为空对象 `{"type": "object", "properties": {}, "required": [], "additionalProperties": false}`（模型不能传入任何命令或参数）。
     - 执行前与执行后均核对 `protected_hashes`（测试文件 `sha256`）：一旦发现测试文件被改动或删除，立即报错 `protected_test_modified`；
     - 使用 `subprocess.run(test_command, cwd=workspace_root, capture_output=True, text=True, timeout=timeout_sec, env=clean_env)` 执行固定测试入口，返回退出码与截断后的标准输出/错误。
- **验收标准**：`tests/test_tools_patch_and_tests.py` 覆盖：正常 propose + apply、修改 `tests/` 被硬拦截、`old_str` 不存在或不唯一被拒、审批拒绝后文件不变、propose 后文件被外部修改导致旧 `patch_id` 失效、`run_tests` 正常捕获红/绿退出码与测试文件篡改检测。

#### PR-07（T2.2）：冻结修复案例集 `cases/`（2 dev + 4 test）与 `MANIFEST.json` 哈希校验器
- **Dependencies**: `PR-05`
- **Branch**: `feat/pr-07-repair-cases-dataset`
- **Worktree**: `/data/miniloop/wt/pr-07`
- **`hot_resources`**: `[]`
- **`allowed_paths`**:
  - `cases/MANIFEST.json`
  - `cases/case_01_off_by_one/**`
  - `cases/case_02_none_guard/**`
  - `cases/case_03_date_window/**`
  - `cases/case_04_csv_quoting/**`
  - `cases/case_05_retry_backoff/**`
  - `cases/case_06_lru_eviction/**`
  - `src/miniloop/cases.py`
  - `tests/test_cases_manifest.py`
- **Scope**:
  1. 构建 6 个独立、自包含的小型 Python 故障修复案例（不通过改文件名凑数，涵盖不同算法/边界缺陷）：
     - **开发集 (`split: "dev"`)**：
       - `case_01_off_by_one`：分页切片器 `paginate(items, page, page_size)` 的尾页边界与页码从 1 计数偏移故障。
       - `case_02_none_guard`：配置合并器 `merge_defaults(user_cfg, defaults)` 在用户显式传入 `0` / `False` / `""` 时被误判为 `None` 覆盖的 falsy 陷阱。
     - **正式评测集 (`split: "test"`)**：
       - `case_03_date_window`：滑动时间窗口统计器在跨日/闭区间边界 `[start, end]` 上的包含性错误。
       - `case_04_csv_quoting`：轻量键值行解析器在值内含转义引号与分隔符时的切分错误。
       - `case_05_retry_backoff`：指数退避计算器 `compute_backoff(attempt, base, max_delay)` 的指数基数错位与上限截断缺失。
       - `case_06_lru_eviction`：简易固定容量缓存 `SimpleLRU.get()` 命中已有键时未刷新访问顺序导致误淘汰热键。
  2. 每个案例目录包含：
     - `case.json`：`case_id`、`split` (`"dev"` | `"test"`)、`task_prompt`、`editable_files`（如 `["src/module.py"]`）、`protected_test_files`（如 `["tests/test_module.py"]`）、`test_command`（`["pytest", "-q"]`）、`reference_fix`（含 `rel_path`、`old_str`、`new_str`，供自检使用，**拷入运行工作区时必须剥离**）。
     - 起始故障源码与 `tests/` 评分测试。
  3. **`cases/MANIFEST.json` 与 `src/miniloop/cases.py`**：
     - 冻结全部 6 个案例所有文件的 `sha256`；
     - `prepare_case_workspace(case, dest_dir)`：将 `editable_files` 与 `protected_test_files` 拷贝到隔离目录 `dest_dir`（绝不写入 `case.json` 中的 `reference_fix`），并返回测试文件哈希表。
     - `verify_case_with_reference_fix(case)`：在临时目录中验证：① 初始状态跑 `pytest` 必须失败（红）；② 应用人工参考补丁后跑 `pytest` 必须通过（绿）且测试文件哈希未变。
- **验收标准**：`tests/test_cases_manifest.py` 校验 `MANIFEST.json` 哈希 100% 匹配，且全部 6 个案例均通过“初始必败 → 人工参考修复必过”验证。

---

### Wave 4 — 独立评测器、真实模型修复评测与可复现交付收官（单 PR 汇合）

#### PR-08（T2.3）：独立评测运行器 `eval_runner`、真实模型评测报告与可复现交付文档
- **Dependencies**: `PR-06`, `PR-07`
- **Branch**: `feat/pr-08-eval-runner-and-delivery`
- **Worktree**: `/data/miniloop/wt/pr-08`
- **`hot_resources`**: `["cli.py", "README.md"]`
- **`allowed_paths`**:
  - `src/miniloop/eval_runner.py`
  - `src/miniloop/cli.py`
  - `scripts/verify.sh`
  - `tests/test_eval_runner.py`
  - `docs/status/evaluation-report.md`
  - `docs/roadmap.md`
  - `REFERENCES.md`
  - `README.md`
- **Scope**:
  1. **`src/miniloop/eval_runner.py`**：
     - 显式分离确定性协议验证与真实模型任务评测；
     - 对每个案例创建独立工作区 `runs/<eval_id>/<case_id>/workspace/` 与轨迹 `runs/<eval_id>/<case_id>/trajectory.jsonl`；
     - 循环结束后，由独立评分器 `evaluate_workspace(case, workspace_dir)`：
       1. 核对 `protected_test_files` 的 `sha256` 与 `MANIFEST.json` 完全一致；
       2. 核对被修改的文件仅限于 `editable_files`；
       3. 独立运行案例预设 `pytest` 入口并检查退出码 `== 0` 且加载测试数 `> 0`；
       4. 分别记录 `exit_reason`（模型是否正常结束循环）与 `repair_succeeded`（代码修复是否真正通过测试）。
     - 汇总指标：`completed_cases / total_cases`、各 `ExitReason` 计数、总模型调用数、总工具调用数、端到端耗时、Token 用量、逐例 diff 与轨迹路径。
  2. **扩展 `src/miniloop/cli.py`**：
     - 新增 `miniloop eval --split dev|test|all --provider openai_compatible [--output-dir runs/...]`（当 `--provider fake` 时仅允许用于单元测试，正式评测报告生成器拒绝把 `fake` 标为真实模型评测）。
  3. **真实模型运行与报告落盘（`docs/status/evaluation-report.md`）**：
     - 使用本机 Magpie 网关上的真实模型先跑 `dev` 集（完成 P0-04 验收），再在冻结条件下跑 `test` 集（完成 P1-02 验收），如实生成 `docs/status/evaluation-report.md`（保留首次完整运行结果，包含成功与失败案例归因）。
  4. **一键验证脚本 `scripts/verify.sh` 与文档更新（P1-03）**：
     - `./scripts/verify.sh --offline`：依次执行 `ruff`、`mypy`、`pytest`、`verify-protocol --check`、6 个案例参考补丁自检与回放零调用断言；
     - 更新 `README.md`、`docs/roadmap.md`（逐项附提交哈希、运行命令与证据路径勾选）、`REFERENCES.md`（如实记录参考概念与本人独立实现部分）。
- **验收标准**：`./scripts/verify.sh --offline` 与真实模式 `./scripts/verify.sh` 全绿；`docs/status/evaluation-report.md` 真实可追溯。

---

## 5. PR 顺序与并行依赖图（DAG）

```text
Wave 0 (串行基线):
  [PR-01: 工具链 + types.py + config/secrets + CI]
         │
         ├───────────────────────────┬───────────────────────────┐
         ▼                           ▼                           ▼
Wave 1 (3 Worktrees 并行):
  [PR-02: 模型适配器 & Fake]  [PR-03: 工具注册表 & read_file]  [PR-04: 预算器 & JSONL/回放]
         │                           │                           │
         └───────────────────────────┼───────────────────────────┘
                                     ▼
Wave 2 (串行汇合):
         [PR-05: 主循环 run_loop + CLI + 10 类协议验证 + P0-01 真实首跑]
                                     │
                     ┌───────────────┴───────────────┐
                     ▼                               ▼
Wave 3 (2 Worktrees 并行):
  [PR-06: propose/apply_patch & run_tests]    [PR-07: 6 个冻结修复案例 & MANIFEST]
                     │                               │
                     └───────────────┬───────────────┘
                                     ▼
Wave 4 (串行收官):
         [PR-08: 独立评测器 eval_runner + 真实模型评测报告 + verify.sh & 文档收官]
```

| PR | 波次 | 任务 | 依赖 | `hot_resources` | 规模 |
| --- | --- | --- | --- | --- | --- |
| PR-01 | Wave 0 | T0.1 工具链、核心类型、`config`/`secrets`、GitHub Actions CI | — | `pyproject.toml`, `types.py`, `config.py` | M |
| PR-02 | Wave 1 | T1.1 `OpenAICompatibleAdapter` 与 `FakeModelAdapter` | PR-01 | `[]` | M |
| PR-03 | Wave 1 | T1.2 `ToolRegistry`、参数 Schema 校验、`read_file` 与只读演示文件 | PR-01 | `[]` | M |
| PR-04 | Wave 1 | T1.3 `BudgetTracker`、脱敏 JSONL 记录器与只读回放器 | PR-01 | `[]` | M |
| PR-05 | Wave 2 | T1.4 主循环 `run_loop`、CLI、10 类协议验证与 P0-01 真实首跑 | PR-02, 03, 04 | `loop.py`, `cli.py` | L |
| PR-06 | Wave 3 | T2.1 `propose_patch` / `apply_patch`（乐观锁）与固定入口 `run_tests` | PR-05 | `tools/__init__.py` | M |
| PR-07 | Wave 3 | T2.2 6 个独立修复案例（`dev` 2 + `test` 4）与 `MANIFEST.json` 校验 | PR-05 | `[]` | M |
| PR-08 | Wave 4 | T2.3 `eval_runner`、真实模型评测报告、`scripts/verify.sh` 与文档收官 | PR-06, 07 | `cli.py`, `README.md` | L |

---

## 6. 配置项总表

| 变量 | 引入 PR | 默认值 | 说明 |
| --- | --- | --- | --- |
| `MINILOOP_MODEL_PROVIDER` | PR-01 | `fake` | `"fake"`（离线协议验证）或 `"openai_compatible"`（真实模型模式） |
| `MINILOOP_MODEL_BASE_URL` | PR-01 | `http://127.0.0.1:3425/v1` | OpenAI 兼容端点地址，默认指向本机 Magpie 网关 |
| `MINILOOP_MODEL_NAME` | PR-01 | 无 | `openai_compatible` 时必填（如 `claude-sonnet-5-5`、`gemini-3.8-flash`） |
| `MINILOOP_MODEL_API_KEY` | PR-01 | 无 | 可选环境变量密钥；与密钥文件二选一，环境变量优先 |
| `MINILOOP_MODEL_API_KEY_FILE` | PR-01 | `~/.config/miniloop/magpie.key` | 密钥文件路径（若默认路径不存在，自动探测 `~/.config/lab-agent/magpie.key`） |
| `MINILOOP_WORKSPACE` | PR-01 | `./workspaces/demo_readonly` | 默认工作区路径 |
| `MINILOOP_MAX_MODEL_CALLS` | PR-01 | `10` | 单次运行最大模型请求次数（含重试） |
| `MINILOOP_MAX_TOOL_CALLS` | PR-01 | `15` | 单次运行最大工具调用次数 |
| `MINILOOP_MAX_WALL_TIME_SEC` | PR-01 | `120.0` | 单次运行总墙钟时间预算（秒） |

---

## 7. 风险与应对

| # | 风险 | 应对措施 |
| --- | --- | --- |
| R1 | GitHub Actions CI 无法访问作者本机 Magpie 网关 | CI 仅运行 `ruff`、`mypy`、`pytest`、`gitleaks` 与 `./scripts/verify.sh --offline`（覆盖全部 10 类确定性协议验证与 6 个案例参考补丁自检）；真实模型评测在宿主机连 Magpie 执行并把脱敏报告提交入库 |
| R2 | 模型尝试修改 `tests/` 测试文件以作弊通过 `run_tests` | 三重防护：① `propose_patch` 白名单仅允许 `editable_files`；② `run_tests` 前后校验测试文件 `sha256`；③ 循环结束后独立评分器再次核对 `MANIFEST.json` 测试文件 `sha256` |
| R3 | 真实模型结束循环并声称“已修复”，但实际未调用 `apply_patch` 或测试未过 | 严格分离 `exit_reason == "completed"` 与 `repair_succeeded == True`，最终评分以独立评分器在工作区副本上重跑固定 `pytest` 的结果为准 |

---

## 8. 简历口径（诚实、可复现）

全部 PR 合入后，所有可写进简历的表述均对应仓库内可一键复现的命令与报告文件：
- **可执行工具调用内核**：使用 Python 3.12 从零实现串行工具调用执行循环 MiniLoop，涵盖 OpenAI 兼容协议适配、零黑盒工具参数 Schema 校验、单轮多工具 `call_id` 精确配对、Deadline 驱动的步数/时间双预算控制，以及 `ToolError`（回填模型自愈）与 `ProtocolError`（立即终止）两级错误隔离。
- **两阶段乐观锁代码修改与防篡改评测**：设计 `propose_patch` / `apply_patch` 基于文件 `sha256` 的乐观锁与审批回调机制，文件变动后旧确认自动失效；结合固定入口 `run_tests` 与测试文件 `sha256` 完整性校验，阻断模型篡改测试作弊。
- **JSONL 脱敏轨迹与零副作用只读回放**：实现 Schema v1.0 事件流记录器（自动过滤密钥与敏感头）及纯只读回放器，支持断号、截断与悬空 `call_id` 诊断，并通过计数替身证明回放过程模型与工具调用次数均为 0。
- **双轨验证（10 类确定性协议验证 + 冻结案例真实模型评测）**：将确定性协议正确性（10/10 场景全绿）与真实大模型任务完成率严格分离报告，在冻结的 Python 故障修复案例集上记录完整轨迹、Token 用量与失败归因。
