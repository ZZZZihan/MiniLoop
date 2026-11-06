"""Environment and key-file configuration loader with strict provider validation."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from miniloop.secrets import SecretLoadError, SecretValue, read_secret
from miniloop.types import BudgetConfig

ProviderName = Literal["fake", "openai_compatible"]

DEFAULT_BASE_URL = "http://127.0.0.1:3425/v1"
DEFAULT_FAKE_MODEL_NAME = "fake-model"
DEFAULT_WORKSPACE = Path("./workspaces/demo_readonly")
DEFAULT_KEY_FILES: tuple[Path, ...] = (
    Path("~/.config/miniloop/magpie.key"),
    Path("~/.config/lab-agent/magpie.key"),
)


class ConfigError(ValueError):
    """Raised when runtime configuration is invalid or missing required fields."""


class _SecretConfigError(SecretLoadError, ConfigError):
    """Raised when secret loading fails during configuration parsing."""


@dataclass(frozen=True)
class ModelConfig:
    """Resolved model, workspace, and budget configuration for a MiniLoop run."""

    provider: ProviderName = "fake"
    base_url: str = DEFAULT_BASE_URL
    model_name: str = DEFAULT_FAKE_MODEL_NAME
    api_key: SecretValue | None = None
    workspace: Path = DEFAULT_WORKSPACE
    budget: BudgetConfig = BudgetConfig()


def _parse_positive_int(raw_value: str | None, var_name: str, default: int) -> int:
    if raw_value is None or not raw_value.strip():
        return default
    try:
        parsed = int(raw_value.strip())
    except ValueError as exc:
        raise ConfigError(f"{var_name} must be an integer, got {raw_value!r}") from exc
    if parsed <= 0:
        raise ConfigError(f"{var_name} must be > 0, got {parsed}")
    return parsed


def _parse_positive_float(raw_value: str | None, var_name: str, default: float) -> float:
    if raw_value is None or not raw_value.strip():
        return default
    try:
        parsed = float(raw_value.strip())
    except ValueError as exc:
        raise ConfigError(f"{var_name} must be a number, got {raw_value!r}") from exc
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise ConfigError(f"{var_name} must be a finite number > 0, got {raw_value!r}")
    return parsed


def load_config(
    environ: Mapping[str, str] | None = None,
    *,
    default_key_files: Sequence[Path] | None = None,
) -> ModelConfig:
    """Load and validate ``ModelConfig`` from environment variables and key files.

    When ``MINILOOP_MODEL_PROVIDER == "openai_compatible"``, both ``MINILOOP_MODEL_NAME``
    and a valid API key are strictly required. Missing values raise ``ConfigError``
    and never silently fall back to ``fake``.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ

    raw_provider = env.get("MINILOOP_MODEL_PROVIDER", "").strip() or "fake"
    if raw_provider not in ("fake", "openai_compatible"):
        raise ConfigError(
            f"Invalid MINILOOP_MODEL_PROVIDER={raw_provider!r}; "
            "expected 'fake' or 'openai_compatible'."
        )
    provider: ProviderName = (
        "openai_compatible" if raw_provider == "openai_compatible" else "fake"
    )

    base_url = env.get("MINILOOP_MODEL_BASE_URL", "").strip() or DEFAULT_BASE_URL
    raw_model_name = env.get("MINILOOP_MODEL_NAME", "").strip()

    raw_workspace = env.get("MINILOOP_WORKSPACE", "").strip()
    workspace = Path(raw_workspace) if raw_workspace else DEFAULT_WORKSPACE

    default_budget = BudgetConfig()
    max_model_calls = _parse_positive_int(
        env.get("MINILOOP_MAX_MODEL_CALLS"),
        "MINILOOP_MAX_MODEL_CALLS",
        default_budget.max_model_calls,
    )
    max_tool_calls = _parse_positive_int(
        env.get("MINILOOP_MAX_TOOL_CALLS"),
        "MINILOOP_MAX_TOOL_CALLS",
        default_budget.max_tool_calls,
    )
    max_wall_time_sec = _parse_positive_float(
        env.get("MINILOOP_MAX_WALL_TIME_SEC"),
        "MINILOOP_MAX_WALL_TIME_SEC",
        default_budget.max_wall_time_sec,
    )
    budget = BudgetConfig(
        max_model_calls=max_model_calls,
        max_tool_calls=max_tool_calls,
        max_wall_time_sec=max_wall_time_sec,
    )

    if default_key_files is not None:
        fallback_files: Sequence[Path] = tuple(default_key_files)
    elif provider == "openai_compatible":
        fallback_files = DEFAULT_KEY_FILES
    else:
        fallback_files = ()

    try:
        api_key = read_secret(
            env_value=env.get("MINILOOP_MODEL_API_KEY"),
            file_path=env.get("MINILOOP_MODEL_API_KEY_FILE"),
            default_files=fallback_files,
            name="MINILOOP_MODEL_API_KEY",
        )
    except SecretLoadError as exc:
        raise _SecretConfigError(str(exc)) from exc

    if provider == "openai_compatible":
        if not raw_model_name:
            raise ConfigError(
                "MINILOOP_MODEL_NAME is required when MINILOOP_MODEL_PROVIDER='openai_compatible'."
            )
        if api_key is None:
            raise ConfigError(
                "API key is required when MINILOOP_MODEL_PROVIDER='openai_compatible'. "
                "Set MINILOOP_MODEL_API_KEY or MINILOOP_MODEL_API_KEY_FILE."
            )
        model_name = raw_model_name
    else:
        model_name = raw_model_name or DEFAULT_FAKE_MODEL_NAME

    return ModelConfig(
        provider=provider,
        base_url=base_url,
        model_name=model_name,
        api_key=api_key,
        workspace=workspace,
        budget=budget,
    )
