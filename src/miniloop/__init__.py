"""MiniLoop core package."""

from __future__ import annotations

from miniloop.config import ModelConfig, load_config
from miniloop.eval_runner import (
    CaseEvalResult,
    EvalSuiteSummary,
    WorkspaceEvalVerdict,
    evaluate_repaired_workspace,
    render_evaluation_report,
    run_case_evaluation,
    run_eval_suite,
)
from miniloop.loop import run_loop
from miniloop.types import BudgetConfig, ExitReason, RunResult

__version__ = "0.1.0.dev0"

__all__ = [
    "BudgetConfig",
    "CaseEvalResult",
    "EvalSuiteSummary",
    "ExitReason",
    "ModelConfig",
    "RunResult",
    "WorkspaceEvalVerdict",
    "__version__",
    "evaluate_repaired_workspace",
    "load_config",
    "render_evaluation_report",
    "run_case_evaluation",
    "run_eval_suite",
    "run_loop",
]
