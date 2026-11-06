"""MiniLoop core package."""

from __future__ import annotations

from miniloop.config import ModelConfig, load_config
from miniloop.loop import run_loop
from miniloop.types import BudgetConfig, ExitReason, RunResult

__version__ = "0.1.0.dev0"

__all__ = [
    "BudgetConfig",
    "ExitReason",
    "ModelConfig",
    "RunResult",
    "__version__",
    "load_config",
    "run_loop",
]
