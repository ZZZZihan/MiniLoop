"""Unit tests for core data contracts, secret encapsulation, and config loading."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from miniloop import ExitReason, ModelConfig, __version__, load_config
from miniloop.config import ConfigError
from miniloop.secrets import SecretLoadError, SecretValue, read_secret
from miniloop.types import (
    BudgetConfig,
    Message,
    ModelTurnResponse,
    RunResult,
    TokenUsage,
    ToolCall,
    ToolResult,
    TrajectoryEvent,
)

SENTINEL_SECRET = "test-miniloop-fake-key-sentinel-999"


def test_secret_value_masks_repr_str_and_format() -> None:
    secret = SecretValue(SENTINEL_SECRET)

    assert repr(secret) == "SecretValue('***')"
    assert str(secret) == "SecretValue('***')"
    assert f"{secret}" == "SecretValue('***')"
    assert f"{secret!r}" == "SecretValue('***')"
    assert SENTINEL_SECRET not in repr(secret)
    assert SENTINEL_SECRET not in str(secret)
    assert secret.get_secret_value() == SENTINEL_SECRET
    assert bool(secret) is True
    assert bool(SecretValue("")) is False
    assert secret == SecretValue(SENTINEL_SECRET)
    assert secret != SecretValue("test-miniloop-fake-key-other")
    assert secret != SENTINEL_SECRET
    assert hash(secret) == hash(SecretValue(SENTINEL_SECRET))


def test_read_secret_precedence_and_fallbacks(tmp_path: Path) -> None:
    explicit_file = tmp_path / "explicit.key"
    explicit_file.write_text("  test-miniloop-fake-key-from-explicit-file\n", encoding="utf-8")

    default_1 = tmp_path / "missing_default.key"
    default_2 = tmp_path / "second_default.key"
    default_2.write_text("test-miniloop-fake-key-from-default-2\n", encoding="utf-8")

    # 1. Environment variable takes top precedence over explicit and default files
    from_env = read_secret(
        env_value=f"  {SENTINEL_SECRET}  ",
        file_path=explicit_file,
        default_files=[default_1, default_2],
    )
    assert from_env is not None
    assert from_env.get_secret_value() == SENTINEL_SECRET

    # 2. Explicit file takes precedence over default files when env_value is empty
    from_explicit = read_secret(
        env_value="   ",
        file_path=str(explicit_file),
        default_files=[default_1, default_2],
    )
    assert from_explicit is not None
    assert from_explicit.get_secret_value() == "test-miniloop-fake-key-from-explicit-file"

    # 3. Probes default_files in order when env_value and file_path are absent
    from_default = read_secret(
        env_value=None,
        file_path=None,
        default_files=[default_1, default_2],
    )
    assert from_default is not None
    assert from_default.get_secret_value() == "test-miniloop-fake-key-from-default-2"

    # 4. Returns None when no source yields a secret
    assert read_secret(default_files=[default_1]) is None


def test_read_secret_missing_or_empty_explicit_file_raises_without_leaking(
    tmp_path: Path,
) -> None:
    missing_file = tmp_path / "does_not_exist.key"
    with pytest.raises(SecretLoadError, match="does not exist"):
        read_secret(file_path=missing_file, name="TEST_KEY")

    empty_file = tmp_path / "empty.key"
    empty_file.write_text("   \n\t  ", encoding="utf-8")
    with pytest.raises(SecretLoadError, match="is empty"):
        read_secret(file_path=empty_file, name="TEST_KEY")

    invalid_utf8_file = tmp_path / "binary.key"
    invalid_utf8_file.write_bytes(b"\xff\xfe" + SENTINEL_SECRET.encode("utf-8"))
    with pytest.raises(SecretLoadError) as exc_info:
        read_secret(file_path=invalid_utf8_file, name="TEST_KEY")
    assert SENTINEL_SECRET not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_load_config_fake_mode_defaults_without_credentials() -> None:
    cfg = load_config({})

    assert cfg.provider == "fake"
    assert cfg.base_url == "http://127.0.0.1:3425/v1"
    assert cfg.model_name == "fake-model"
    assert cfg.api_key is None
    assert cfg.workspace == Path("./workspaces/demo_readonly")
    assert cfg.budget == BudgetConfig(
        max_model_calls=10,
        max_tool_calls=15,
        max_wall_time_sec=120.0,
    )
    assert __version__ == "0.1.0.dev0"


def test_load_config_openai_compatible_success_and_repr_redaction(tmp_path: Path) -> None:
    key_file = tmp_path / "magpie.key"
    key_file.write_text(f"{SENTINEL_SECRET}\n", encoding="utf-8")

    cfg = load_config(
        {
            "MINILOOP_MODEL_PROVIDER": "openai_compatible",
            "MINILOOP_MODEL_BASE_URL": "http://127.0.0.1:3425/v1",
            "MINILOOP_MODEL_NAME": "claude-sonnet-5-5",
            "MINILOOP_WORKSPACE": "/tmp/custom_ws",
            "MINILOOP_MAX_MODEL_CALLS": "6",
            "MINILOOP_MAX_TOOL_CALLS": "8",
            "MINILOOP_MAX_WALL_TIME_SEC": "45.5",
        },
        default_key_files=[key_file],
    )

    assert cfg.provider == "openai_compatible"
    assert cfg.model_name == "claude-sonnet-5-5"
    assert cfg.api_key is not None
    assert cfg.api_key.get_secret_value() == SENTINEL_SECRET
    assert SENTINEL_SECRET not in repr(cfg)
    assert SENTINEL_SECRET not in str(cfg)
    assert cfg.workspace == Path("/tmp/custom_ws")
    assert cfg.budget.max_model_calls == 6
    assert cfg.budget.max_tool_calls == 8
    assert cfg.budget.max_wall_time_sec == 45.5


def test_load_config_openai_compatible_never_falls_back_to_fake() -> None:
    # Missing MINILOOP_MODEL_NAME
    with pytest.raises(ConfigError, match="MINILOOP_MODEL_NAME"):
        load_config(
            {
                "MINILOOP_MODEL_PROVIDER": "openai_compatible",
                "MINILOOP_MODEL_API_KEY": SENTINEL_SECRET,
            },
            default_key_files=[],
        )

    # Missing API key (both env and key files absent)
    with pytest.raises(ConfigError, match="API key is required"):
        load_config(
            {
                "MINILOOP_MODEL_PROVIDER": "openai_compatible",
                "MINILOOP_MODEL_NAME": "claude-sonnet-5-5",
            },
            default_key_files=[],
        )


def test_load_config_rejects_invalid_provider_and_budgets() -> None:
    with pytest.raises(ConfigError, match="MINILOOP_MODEL_PROVIDER"):
        load_config({"MINILOOP_MODEL_PROVIDER": "unknown_provider"})

    with pytest.raises(ConfigError, match="MINILOOP_MAX_MODEL_CALLS"):
        load_config({"MINILOOP_MAX_MODEL_CALLS": "0"})

    with pytest.raises(ConfigError, match="MINILOOP_MAX_TOOL_CALLS"):
        load_config({"MINILOOP_MAX_TOOL_CALLS": "not-an-int"})

    with pytest.raises(ConfigError, match="MINILOOP_MAX_WALL_TIME_SEC"):
        load_config({"MINILOOP_MAX_WALL_TIME_SEC": "-5"})

    with pytest.raises(ConfigError, match="MINILOOP_MAX_WALL_TIME_SEC"):
        load_config({"MINILOOP_MAX_WALL_TIME_SEC": "nan"})


def test_core_types_and_exit_reason_immutability() -> None:
    assert [reason.value for reason in ExitReason] == [
        "completed",
        "step_limit",
        "time_limit",
        "model_error",
        "protocol_error",
        "cancelled",
        "runtime_error",
    ]

    call = ToolCall(id="call_1", name="read_file", arguments={"path": "README.md"})
    result = ToolResult(call_id="call_1", tool_name="read_file", content="hello")
    usage = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    msg = Message(role="assistant", content=None, tool_calls=(call,))
    turn = ModelTurnResponse(
        content="done",
        tool_calls=(call,),
        usage=usage,
        model_id="fake-model",
        raw_response={"id": "resp_1"},
    )
    budget = BudgetConfig()
    event = TrajectoryEvent(
        schema_version="1.0",
        run_id="run_01",
        sequence=1,
        timestamp="2026-11-06T00:00:00Z",
        event_type="run_start",
        payload={"task": "demo"},
    )
    run_res = RunResult(
        run_id="run_01",
        exit_reason=ExitReason.COMPLETED,
        final_answer="fixed",
        model_calls=2,
        tool_calls=1,
        wall_time_sec=1.25,
    )
    cfg = ModelConfig()

    for instance, attr, val in [
        (call, "name", "other"),
        (result, "content", "other"),
        (usage, "total_tokens", 99),
        (msg, "content", "mutated"),
        (turn, "model_id", "mutated"),
        (budget, "max_model_calls", 99),
        (event, "sequence", 2),
        (run_res, "model_calls", 99),
        (cfg, "model_name", "mutated"),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(instance, attr, val)
