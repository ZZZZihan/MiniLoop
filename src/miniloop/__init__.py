"""MiniLoop core package."""

from __future__ import annotations

from miniloop.config import ModelConfig, load_config
from miniloop.types import ExitReason

__version__ = "0.1.0.dev0"

__all__ = [
    "ExitReason",
    "ModelConfig",
    "__version__",
    "load_config",
]
