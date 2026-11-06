# MiniLoop Dual-Track Verification & Repair Evaluation Report

This report strictly separates **Track A (Deterministic Protocol Verification)** (using `FakeModelAdapter`, zero network) from **Track B (Code Repair Task Evaluation)**, and separates loop termination (`exit_reason == 'completed'`) from independent workspace verification (`repair_succeeded == True`).

## Execution Metadata & Frozen Configuration

- **Evaluation ID**: `eval_all_20261106T154738Z`
- **Timestamp (UTC)**: `2026-11-06T15:47:38.033776+00:00`
- **Git Commit**: `bf4f2a1`
- **Split Evaluated**: `all`
- **Provider**: `openai_compatible`
- **Model Identifier**: `remote-magpie/auto/claude-sonnet-5-5`
- **Endpoint Base URL**: `http://127.0.0.1:3425/v1`
- **Cases MANIFEST.json SHA-256 Verified**: `True`
- **Budget Configuration**: `max_model_calls=12`, `max_tool_calls=20`, `max_wall_time_sec=180.0`, `per_request_timeout_sec=30.0`, `per_tool_timeout_sec=15.0`, `max_model_retries=2`
- **Approval Policy**: Fixed whitelist auto-approval (`approve_all_allowed`) restricted strictly to each case's `editable_files` (`tests/` paths are hard-blocked).
- **Artifacts Directory**: `runs/eval_all_claude_sonnet_5_5`

---

## 1. Track A — Deterministic Protocol Verification Suite (`FakeModelAdapter`)

Command: `uv run python -m miniloop.cli verify-protocol --check`

| ID | Scenario | Status | Verified Behavior & Side-Effect Assertion |
| --- | --- | --- | --- |
| `S01` | `S01_normal_completion` | **PASS** | exit_reason=completed, model_calls=2, tool_calls=1, call_id paired |
| `S02` | `S02_invalid_arguments_no_side_effect` | **PASS** | side_effect_counter=0 on invalid_arguments, recovered on turn 2 |
| `S03` | `S03_unknown_tool_handled` | **PASS** | unknown_tool reported cleanly with zero side effects |
| `S04` | `S04_tool_timeout` | **PASS** | per-tool timeout (error_code=tool_timeout) and wall-clock time_limit verified |
| `S05` | `S05_model_error_bounded_retry` | **PASS** | transient errors retried 2x (model_calls=3) then exited with model_error |
| `S06` | `S06_infinite_tool_loop_step_limit` | **PASS** | halted at exact max_tool_calls=3 and max_model_calls=3 with step_limit |
| `S07` | `S07_rejected_modification_preserves_workspace` | **PASS** | workspace SHA-256 hashes 100% unchanged after rejected escape & modification |
| `S08` | `S08_missing_or_duplicate_call_id_protocol_error` | **PASS** | missing and same/cross-turn duplicate call_id halted with protocol_error |
| `S09` | `S09_multi_tool_calls_ordered_pairing` | **PASS** | call_a and call_b executed serially in order and paired to distinct tool messages |
| `S10` | `S10_readonly_replay_zero_execution` | **PASS** | replay model_calls=0, tool_executions=0, truncated trajectory rejected |

**Track A Summary**: `10/10` deterministic protocol scenarios passed (`ALL PASSED`).

---

## 2. Track B — Real-Model Frozen Repair Benchmark Evaluation

### 2.1 Aggregate Metrics

| Metric | Value |
| --- | --- |
| **Overall Repaired (`repair_succeeded`)** | **6 / 6** |
| **Dev Split Repaired (`dev`)** | `2 / 2` |
| **Formal Test Split Repaired (`test`)** | `4 / 4` |
| **Loop Normal Termination (`exit_reason=completed`)** | `6 / 6` |
| **Exit Reason Breakdown** | `completed`: 6 |
| **Total Model Calls** | `33` |
| **Total Tool Calls** | `34` |
| **Total Wall-Clock Time** | `76.30s` |
| **Total Token Usage** | `96649 (p=91927, c=4722)` |

### 2.2 Per-Case Results

| Case ID | Split | Loop Exit (`exit_reason`) | Independent Repair (`repair_succeeded`) | Test Files Intact | Allowed Scope Only | Model Calls | Tool Calls | Wall Time (s) | Tokens (total / p / c) | Failure Classification | Trajectory |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `case_01_off_by_one` | `dev` | `completed` | **True** | `True` | `True` | `5` | `5` | `8.13` | `11875 (p=11452, c=423)` | `none (repaired)` | `runs/eval_all_claude_sonnet_5_5/case_01_off_by_one/trajectory.jsonl` |
| `case_02_none_guard` | `dev` | `completed` | **True** | `True` | `True` | `5` | `5` | `13.44` | `12803 (p=12296, c=507)` | `none (repaired)` | `runs/eval_all_claude_sonnet_5_5/case_02_none_guard/trajectory.jsonl` |
| `case_03_date_window` | `test` | `completed` | **True** | `True` | `True` | `5` | `5` | `12.28` | `11533 (p=10946, c=587)` | `none (repaired)` | `runs/eval_all_claude_sonnet_5_5/case_03_date_window/trajectory.jsonl` |
| `case_04_csv_quoting` | `test` | `completed` | **True** | `True` | `True` | `5` | `5` | `15.39` | `18163 (p=16469, c=1694)` | `none (repaired)` | `runs/eval_all_claude_sonnet_5_5/case_04_csv_quoting/trajectory.jsonl` |
| `case_05_retry_backoff` | `test` | `completed` | **True** | `True` | `True` | `5` | `5` | `9.16` | `12797 (p=12263, c=534)` | `none (repaired)` | `runs/eval_all_claude_sonnet_5_5/case_05_retry_backoff/trajectory.jsonl` |
| `case_06_lru_eviction` | `test` | `completed` | **True** | `True` | `True` | `8` | `9` | `17.91` | `29478 (p=28501, c=977)` | `none (repaired)` | `runs/eval_all_claude_sonnet_5_5/case_06_lru_eviction/trajectory.jsonl` |

### 2.3 Per-Case Diffs, Independent Verification & Failure Attribution

#### `case_01_off_by_one` (dev) — 1-based pagination slice offset and empty page validation

- **Run ID**: `eval_case_01_off_by_one`
- **Loop Exit Reason**: `completed`
- **Independent Repair Succeeded**: `True`
- **Modified Files**: `['src/paginator.py']`
- **Protected Test Hash Intact**: `True`
- **Independent Pytest Exit Code**: `0` (`.....                                                                    [100%]
5 passed in 0.05s`)
- **Failure Classification**: `none (repaired)`
- **Trajectory**: `runs/eval_all_claude_sonnet_5_5/case_01_off_by_one/trajectory.jsonl`
- **Recorded Tool Exchanges (`5` total, `0` tool-level error(s) recovered)**:
  - `[4->5]` `toolu_vrtx_01A1Ku1XeuE3wjvP3YncAspz` -> `read_file` -> OK
  - `[6->7]` `toolu_vrtx_01BMu5Z3HEWbF6CZF2P9Jwb8` -> `read_file` -> OK
  - `[10->11]` `toolu_vrtx_019p8sCST2RLdUcqmBT1GmST` -> `propose_patch` -> OK
  - `[14->15]` `toolu_vrtx_01K346QcDKKZYPUe6UMMC9o1` -> `apply_patch` -> OK
  - `[18->19]` `toolu_vrtx_014jgnBLFbhGWCYRNvHvmdjZ` -> `run_tests` -> OK

```diff
--- a/src/paginator.py
+++ b/src/paginator.py
@@ -16,6 +16,6 @@
     if page_size < 1:
         raise ValueError("page_size must be >= 1")
 
-    start = page * page_size
+    start = (page - 1) * page_size
     end = start + page_size
     return list(items[start:end])
```

#### `case_02_none_guard` (dev) — Preserve explicit falsy overrides (0, False, empty string) when merging configuration

- **Run ID**: `eval_case_02_none_guard`
- **Loop Exit Reason**: `completed`
- **Independent Repair Succeeded**: `True`
- **Modified Files**: `['src/config_merge.py']`
- **Protected Test Hash Intact**: `True`
- **Independent Pytest Exit Code**: `0` (`....                                                                     [100%]
4 passed in 0.04s`)
- **Failure Classification**: `none (repaired)`
- **Trajectory**: `runs/eval_all_claude_sonnet_5_5/case_02_none_guard/trajectory.jsonl`
- **Recorded Tool Exchanges (`5` total, `0` tool-level error(s) recovered)**:
  - `[4->5]` `toolu_vrtx_01QYNv54mhEMf9UodByR8haR` -> `read_file` -> OK
  - `[6->7]` `toolu_vrtx_01TgE6EE15t14FJDoLVNVhs9` -> `read_file` -> OK
  - `[10->11]` `toolu_vrtx_01E8cv8hqpJ9vru8BPus8NyS` -> `propose_patch` -> OK
  - `[14->15]` `toolu_vrtx_01NsaxFTStoRKaysppmgdYPG` -> `apply_patch` -> OK
  - `[18->19]` `toolu_vrtx_01UAtVNrMu2a9jhJnT545gkL` -> `run_tests` -> OK

```diff
--- a/src/config_merge.py
+++ b/src/config_merge.py
@@ -18,7 +18,8 @@
     """
     merged: dict[str, Any] = {}
     for k, v in defaults.items():
-        merged[k] = overrides.get(k) or v
+        override = overrides.get(k)
+        merged[k] = v if override is None else override
     for k, val in overrides.items():
         if k not in merged and val is not None:
             merged[k] = val
```

#### `case_03_date_window` (test) — Closed interval [start_ts, end_ts] boundary inclusion and range validation

- **Run ID**: `eval_case_03_date_window`
- **Loop Exit Reason**: `completed`
- **Independent Repair Succeeded**: `True`
- **Modified Files**: `['src/window_counter.py']`
- **Protected Test Hash Intact**: `True`
- **Independent Pytest Exit Code**: `0` (`....                                                                     [100%]
4 passed in 0.04s`)
- **Failure Classification**: `none (repaired)`
- **Trajectory**: `runs/eval_all_claude_sonnet_5_5/case_03_date_window/trajectory.jsonl`
- **Recorded Tool Exchanges (`5` total, `0` tool-level error(s) recovered)**:
  - `[4->5]` `toolu_vrtx_017nW8HpHegs81RfmSowK1Dy` -> `read_file` -> OK
  - `[6->7]` `toolu_vrtx_016WarUvvy2ZiUpKQGXn6hcj` -> `read_file` -> OK
  - `[10->11]` `toolu_vrtx_01FNSk4S49ziwHVC5LggFcAM` -> `propose_patch` -> OK
  - `[14->15]` `toolu_vrtx_019VTECJSwHLJY136r9ThHnL` -> `apply_patch` -> OK
  - `[18->19]` `toolu_vrtx_01AgS7tJYopF4q2D61tuXYoG` -> `run_tests` -> OK

```diff
--- a/src/window_counter.py
+++ b/src/window_counter.py
@@ -14,4 +14,6 @@
 
     Raises ValueError if start_ts > end_ts.
     """
-    return sum(1 for ts in timestamps if start_ts < ts < end_ts)
+    if start_ts > end_ts:
+        raise ValueError("start_ts must be <= end_ts")
+    return sum(1 for ts in timestamps if start_ts <= ts <= end_ts)
```

#### `case_04_csv_quoting` (test) — Parse comma-separated key=value pairs with double-quoted values containing commas

- **Run ID**: `eval_case_04_csv_quoting`
- **Loop Exit Reason**: `completed`
- **Independent Repair Succeeded**: `True`
- **Modified Files**: `['src/kv_parser.py']`
- **Protected Test Hash Intact**: `True`
- **Independent Pytest Exit Code**: `0` (`....                                                                     [100%]
4 passed in 0.05s`)
- **Failure Classification**: `none (repaired)`
- **Trajectory**: `runs/eval_all_claude_sonnet_5_5/case_04_csv_quoting/trajectory.jsonl`
- **Recorded Tool Exchanges (`5` total, `0` tool-level error(s) recovered)**:
  - `[4->5]` `toolu_vrtx_012Q3wrRibDDetCEnF6QSCGV` -> `read_file` -> OK
  - `[6->7]` `toolu_vrtx_01JfdCNV8mYzbnZ1sfFvYegw` -> `read_file` -> OK
  - `[10->11]` `toolu_vrtx_013D9i5ZHTjY2WciyDgmXS24` -> `propose_patch` -> OK
  - `[14->15]` `toolu_vrtx_01HCcRwtQLNT1PdigNP6MkYV` -> `apply_patch` -> OK
  - `[18->19]` `toolu_vrtx_01SyRbfopFNSr73ZAvLXTaYA` -> `run_tests` -> OK

```diff
--- a/src/kv_parser.py
+++ b/src/kv_parser.py
@@ -17,12 +17,58 @@
         return {}
 
     result: dict[str, str] = {}
-    for part in line.split(","):
-        if "=" not in part:
-            raise ValueError(f"Missing '=' in segment: {part!r}")
-        key, raw_val = part.split("=", 1)
-        key = key.strip()
+    i = 0
+    n = len(line)
+    while True:
+        # Parse key: up to '=' (a comma or end before '=' means missing '=').
+        start = i
+        while i < n and line[i] not in "=,":
+            i += 1
+        if i >= n or line[i] == ",":
+            raise ValueError(f"Missing '=' in segment: {line[start:i]!r}")
+        key = line[start:i].strip()
         if not key:
             raise ValueError("Key must not be empty")
-        result[key] = raw_val.strip()
+        i += 1  # skip '='
+
+        while i < n and line[i].isspace():
+            i += 1
+
+        if i < n and line[i] == '"':
+            i += 1
+            chars: list[str] = []
+            closed = False
+            while i < n:
+                c = line[i]
+                if c == "\\" and i + 1 < n and line[i + 1] == '"':
+                    chars.append('"')
+                    i += 2
+                elif c == '"':
+                    if i + 1 < n and line[i + 1] == '"':
+                        chars.append('"')
+                        i += 2
+                    else:
+                        i += 1
+                        closed = True
+                        break
+                else:
+                    chars.append(c)
+                    i += 1
+            if not closed:
+                raise ValueError("Unclosed quote")
+            while i < n and line[i].isspace():
+                i += 1
+            if i < n and line[i] != ",":
+                raise ValueError("Unexpected characters after closing quote")
+            value = "".join(chars)
+        else:
+            start = i
+            while i < n and line[i] != ",":
+                i += 1
+            value = line[start:i].strip()
+
+        result[key] = value
+        if i >= n:
+            break
+        i += 1  # skip ','
     return result
```

#### `case_05_retry_backoff` (test) — 1-based exponential retry backoff exponent and max_delay ceiling

- **Run ID**: `eval_case_05_retry_backoff`
- **Loop Exit Reason**: `completed`
- **Independent Repair Succeeded**: `True`
- **Modified Files**: `['src/backoff.py']`
- **Protected Test Hash Intact**: `True`
- **Independent Pytest Exit Code**: `0` (`....                                                                     [100%]
4 passed in 0.04s`)
- **Failure Classification**: `none (repaired)`
- **Trajectory**: `runs/eval_all_claude_sonnet_5_5/case_05_retry_backoff/trajectory.jsonl`
- **Recorded Tool Exchanges (`5` total, `0` tool-level error(s) recovered)**:
  - `[4->5]` `toolu_vrtx_01WXPZMuaBrmFBp5GzhogM9R` -> `read_file` -> OK
  - `[6->7]` `toolu_vrtx_01FrcPv2miSpSW8PYsAtH5mJ` -> `read_file` -> OK
  - `[10->11]` `toolu_vrtx_01YDAaBqFEjdSyYcDwJYRXeJ` -> `propose_patch` -> OK
  - `[14->15]` `toolu_vrtx_01XxQbthY6SMzc5jBaPB4mv2` -> `apply_patch` -> OK
  - `[18->19]` `toolu_vrtx_01KCXWYKu76PcDrQ9yRfykdn` -> `run_tests` -> OK

```diff
--- a/src/backoff.py
+++ b/src/backoff.py
@@ -19,8 +19,8 @@
     if base_delay <= 0 or max_delay <= 0:
         raise ValueError("base_delay and max_delay must be > 0")
 
-    delay = base_delay * (2 ** attempt)
-    return float(delay)
+    delay = base_delay * (2 ** (attempt - 1))
+    return float(min(delay, max_delay))
 
 
 compute_backoff = compute_backoff_delay
```

#### `case_06_lru_eviction` (test) — Refresh key recency on get() and existing-key put() in SimpleLRU

- **Run ID**: `eval_case_06_lru_eviction`
- **Loop Exit Reason**: `completed`
- **Independent Repair Succeeded**: `True`
- **Modified Files**: `['src/simple_lru.py']`
- **Protected Test Hash Intact**: `True`
- **Independent Pytest Exit Code**: `0` (`....                                                                     [100%]
4 passed in 0.05s`)
- **Failure Classification**: `none (repaired)`
- **Trajectory**: `runs/eval_all_claude_sonnet_5_5/case_06_lru_eviction/trajectory.jsonl`
- **Recorded Tool Exchanges (`9` total, `1` tool-level error(s) recovered)**:
  - `[4->5]` `toolu_vrtx_01EFH2MwSWTFVPaB824YvjZg` -> `read_file` -> OK
  - `[6->7]` `toolu_vrtx_01XGevNUXQJ3TrwoAqWgDP21` -> `read_file` -> OK
  - `[10->11]` `toolu_vrtx_01DxgfWVbobfUE9Lwn1Hs2i1` -> `propose_patch` -> OK
  - `[12->13]` `toolu_vrtx_01BnMHhh4C1x2m6Ve2iGwVpZ` -> `propose_patch` -> OK
  - `[16->17]` `toolu_vrtx_01H71UZMndssgpAVVNXnT17T` -> `apply_patch` -> OK
  - `[20->21]` `toolu_vrtx_0176PtfSDddUPzEAUsCySHgT` -> `apply_patch` -> ERROR `patch_stale_file_changed`
  - `[24->25]` `toolu_vrtx_011wLRqmcN81RRP21oSKDqYp` -> `propose_patch` -> OK
  - `[28->29]` `toolu_vrtx_01Ki5izUxSGF9iPVvKS9R8wY` -> `apply_patch` -> OK
  - `[32->33]` `toolu_vrtx_01SV67WqqZqSWH3xviRQ3xTH` -> `run_tests` -> OK

```diff
--- a/src/simple_lru.py
+++ b/src/simple_lru.py
@@ -25,12 +25,14 @@
         """Return the value for key if present and mark it as most recently used."""
         if key not in self._store:
             return default
+        self._store.move_to_end(key)
         return self._store[key]
 
     def put(self, key: K, value: V) -> None:
         """Insert or update key with value, evicting the least recently used item if full."""
         if key in self._store:
             self._store[key] = value
+            self._store.move_to_end(key)
             return
         if len(self._store) >= self._capacity:
             self._store.popitem(last=False)
```

---

## 3. Scope & Limitations

- **Small Frozen Benchmark Scope**: Results reflect first-run performance on the 6 frozen Python repair cases (`2 dev` + `4 test`) defined in `cases/MANIFEST.json`. They do not claim generalization to arbitrary repositories or external leaderboards.
- **Strict Separation of Loop Completion vs. Repair Success**: `exit_reason='completed'` only indicates the model stopped requesting tools; `repair_succeeded=True` requires independent post-run `pytest` execution (`returncode == 0`), intact SHA-256 hashes on all `protected_test_files`, and modifications restricted to `editable_files`.
