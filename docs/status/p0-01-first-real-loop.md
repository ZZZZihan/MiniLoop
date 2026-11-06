# P0-01 First Real-Model Read-Only Loop Verification Report

- **Date (UTC)**: `2026-11-06`
- **Branch**: `feat/pr-05-loop-cli-protocol-verify`
- **Base Merge Commit**: `d294717`
- **Provider**: `openai_compatible` (Local Magpie Gateway `http://127.0.0.1:3425/v1`)
- **Model Identifier**: `remote-magpie/auto/claude-sonnet-5-5`
- **Workspace**: `./workspaces/demo_readonly`
- **Run ID**: `run_0f448f080a45`
- **Exit Reason**: `completed`

---

## 1. Execution Command

```bash
uv run python -m miniloop.cli run \
  --provider openai_compatible \
  --base-url http://127.0.0.1:3425/v1 \
  --model remote-magpie/auto/claude-sonnet-5-5 \
  --api-key-file ~/.config/lab-agent/magpie.key \
  --workspace ./workspaces/demo_readonly \
  --task "Please read README_DEMO.md and config_sample.ini in the workspace using read_file, and report the service codename, maintenance window, and listen_port." \
  --output /tmp/p0_01_real_run.jsonl
```

---

## 2. CLI RunResult Summary Output

```json
{
  "run_id": "run_0f448f080a45",
  "exit_reason": "completed",
  "final_answer": "- **Service codename:** `AURORA-7`. Both files give this value.\n- **Maintenance window:** `UTC 03:30`. The README describes it as a daily 15-minute drain window. The config file gives the same time.\n- **listen_port:** `18420`. This is in the `[network]` section of `config_sample.ini`.",
  "model_calls": 2,
  "tool_calls": 2,
  "wall_time_sec": 3.4714,
  "trajectory_path": "/tmp/p0_01_real_run.jsonl",
  "error_detail": null
}
```

---

## 3. Sanitized Schema v1.0 Trajectory Events (`run_0f448f080a45`)

| Sequence | Event Type | Summary & Pairing |
| --- | --- | --- |
| `1` | `run_start` | `provider="openai_compatible"`, `model_name="remote-magpie/auto/claude-sonnet-5-5"`, `tools=["read_file"]`, `budget={"max_model_calls": 10, "max_tool_calls": 15, "max_wall_time_sec": 120.0, ...}` |
| `2` | `model_request` | `turn=1`, `message_count=1`, `timeout_sec=30.0`, `max_retries=2` |
| `3` | `model_response` | Returned 2 `read_file` tool calls (`toolu_vrtx_01Hb5Rmux1DX8h54Pou1NeJV` and `toolu_vrtx_01BhYp1mqgeXtZaF9tuMELs8`), `usage={"prompt_tokens": 445, "completion_tokens": 104, "total_tokens": 549}`, `duration_ms=1493.09`, `attempts=1` |
| `4` | `tool_call` | `call_id="toolu_vrtx_01Hb5Rmux1DX8h54Pou1NeJV"`, `tool_name="read_file"`, `arguments={"path": "README_DEMO.md"}` |
| `5` | `tool_result` | Paired `call_id="toolu_vrtx_01Hb5Rmux1DX8h54Pou1NeJV"`, `is_error=false`, `error_code=null`, `duration_ms=4.13` (content of `README_DEMO.md` with `AURORA-7` and `UTC 03:30`) |
| `6` | `tool_call` | `call_id="toolu_vrtx_01BhYp1mqgeXtZaF9tuMELs8"`, `tool_name="read_file"`, `arguments={"path": "config_sample.ini"}` |
| `7` | `tool_result` | Paired `call_id="toolu_vrtx_01BhYp1mqgeXtZaF9tuMELs8"`, `is_error=false`, `error_code=null`, `duration_ms=1.00` (content of `config_sample.ini` with `listen_port = 18420`) |
| `8` | `model_request` | `turn=2`, `message_count=4` (1 user + 1 assistant + 2 paired tool messages), `timeout_sec=30.0`, `max_retries=2` |
| `9` | `model_response` | Final answer with `tool_calls=[]`, `usage={"prompt_tokens": 994, "completion_tokens": 109, "total_tokens": 1103}`, `duration_ms=1967.82`, `attempts=1` |
| `10` | `run_end` | `exit_reason="completed"`, `model_calls=2`, `tool_calls=2`, `wall_time_sec=3.4711` |

---

## 4. Read-Only Replay Output (`miniloop replay`)

```text
Run ID: run_0f448f080a45 (schema v1.0)
Total events: 10 | model_calls: 2 | tool_calls: 2
Tool exchanges:
  [4->5] toolu_vrtx_01Hb5Rmux1DX8h54Pou1NeJV -> read_file({"path": "README_DEMO.md"}) -> result: # MiniLoop Read-Only Demo Workspace ...
  [6->7] toolu_vrtx_01BhYp1mqgeXtZaF9tuMELs8 -> read_file({"path": "config_sample.ini"}) -> result: [service] codename = AURORA-7 ... listen_port = 18420 ...
Exit reason: completed
Final answer: - **Service codename:** `AURORA-7`. Both files give this value.
- **Maintenance window:** `UTC 03:30`. The README describes it as a daily 15-minute drain window. The config file gives the same time.
- **listen_port:** `18420`. This is in the `[network]` section of `config_sample.ini`.
```

---

## 5. Verification Checklist

- [x] Real model (`remote-magpie/auto/claude-sonnet-5-5` via local Magpie gateway `http://127.0.0.1:3425/v1`) emitted structured `read_file` tool calls for both `README_DEMO.md` and `config_sample.ini`.
- [x] `ToolRegistry` validated arguments against `READ_FILE_PARAMETERS_SCHEMA`, verified paths remained inside `./workspaces/demo_readonly`, read file contents, and paired each `ToolResult` with its exact `call_id`.
- [x] Model synthesized the tool outputs and reported `AURORA-7`, `UTC 03:30`, and `18420`, exiting with `exit_reason="completed"`.
- [x] `replay_trajectory` verified continuous sequence numbers (`1..10`), closed `call_id` pairs (`[4->5]`, `[6->7]`), and zero API key or header leakage in the recorded JSONL trajectory.
