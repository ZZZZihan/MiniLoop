"""Command-line interface for MiniLoop protocol verification, execution, and replay."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from miniloop.adapter import (
    FakeStep,
    build_adapter,
    make_text_turn,
    make_tool_call_turn,
)
from miniloop.config import ConfigError, load_config
from miniloop.loop import run_loop
from miniloop.protocol_verify import format_protocol_report, run_protocol_suite
from miniloop.tools import ToolRegistry, build_read_file_tool
from miniloop.trajectory import (
    TrajectoryCorruptedError,
    format_replay_report,
    replay_trajectory,
)
from miniloop.types import ExitReason


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="miniloop",
        description="MiniLoop serial tool-calling execution kernel and trajectory replayer.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # verify-protocol
    verify_parser = subparsers.add_parser(
        "verify-protocol",
        help="Run the 10 deterministic protocol verification scenarios.",
    )
    verify_parser.add_argument(
        "--check",
        action="store_true",
        help="Exit with code 1 if any protocol scenario fails.",
    )

    # run
    run_parser = subparsers.add_parser(
        "run",
        help="Execute a single serial tool-calling loop in a workspace.",
    )
    run_parser.add_argument(
        "--task",
        required=True,
        help="User task prompt for the execution loop.",
    )
    run_parser.add_argument(
        "--workspace",
        default=None,
        help=(
            "Target workspace directory "
            "(defaults to MINILOOP_WORKSPACE or ./workspaces/demo_readonly)."
        ),
    )
    run_parser.add_argument(
        "--provider",
        choices=("fake", "openai_compatible"),
        default=None,
        help="Model provider ('fake' or 'openai_compatible').",
    )
    run_parser.add_argument(
        "--model",
        default=None,
        help="Model identifier (required when provider is 'openai_compatible').",
    )
    run_parser.add_argument(
        "--base-url",
        default=None,
        help="OpenAI-compatible base URL (defaults to http://127.0.0.1:3425/v1).",
    )
    run_parser.add_argument(
        "--api-key-file",
        default=None,
        help="Explicit path to API key file.",
    )
    run_parser.add_argument(
        "--output",
        default=None,
        help="Optional path to write sanitized Schema v1.0 JSONL trajectory.",
    )

    # replay
    replay_parser = subparsers.add_parser(
        "replay",
        help="Replay and validate a recorded JSONL trajectory in pure read-only mode.",
    )
    replay_parser.add_argument(
        "jsonl_path",
        help="Path to the recorded JSONL trajectory file.",
    )

    return parser


def _default_fake_steps_for_workspace(workspace: Path) -> list[FakeStep]:
    """Provide deterministic scripted steps when running CLI in fake mode."""
    readme = workspace / "README_DEMO.md"
    cfg = workspace / "config_sample.ini"
    if readme.is_file() and cfg.is_file():
        return [
            make_tool_call_turn(
                [
                    ("call_fake_readme", "read_file", {"path": "README_DEMO.md"}),
                    ("call_fake_config", "read_file", {"path": "config_sample.ini"}),
                ]
            ),
            make_text_turn(
                "Deterministic fake run: codename=AURORA-7, "
                "maintenance_window=UTC 03:30, listen_port=18420."
            ),
        ]
    if readme.is_file():
        return [
            make_tool_call_turn(
                [("call_fake_readme", "read_file", {"path": "README_DEMO.md"})]
            ),
            make_text_turn("Deterministic fake run completed after reading README_DEMO.md."),
        ]
    return [make_text_turn("Deterministic fake run completed.")]


def _handle_verify_protocol(check: bool) -> int:
    report = run_protocol_suite()
    print(format_protocol_report(report))
    if check and not report.all_passed:
        return 1
    return 0


def _handle_run(args: argparse.Namespace) -> int:
    env = dict(os.environ)
    if args.provider is not None:
        env["MINILOOP_MODEL_PROVIDER"] = str(args.provider)
    if args.model is not None:
        env["MINILOOP_MODEL_NAME"] = str(args.model)
    if args.workspace is not None:
        env["MINILOOP_WORKSPACE"] = str(args.workspace)
    if args.base_url is not None:
        env["MINILOOP_MODEL_BASE_URL"] = str(args.base_url)
    if args.api_key_file is not None:
        env["MINILOOP_MODEL_API_KEY_FILE"] = str(args.api_key_file)

    try:
        config = load_config(env)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    workspace_path = config.workspace
    if not workspace_path.exists() or not workspace_path.is_dir():
        print(
            f"Workspace error: directory does not exist: {workspace_path}",
            file=sys.stderr,
        )
        return 1

    fake_steps: Sequence[FakeStep] = (
        _default_fake_steps_for_workspace(workspace_path)
        if config.provider == "fake"
        else ()
    )

    try:
        adapter = build_adapter(config, fake_steps=fake_steps)
    except ConfigError as exc:
        print(f"Adapter error: {exc}", file=sys.stderr)
        return 1

    registry = ToolRegistry([build_read_file_tool(workspace_path)])
    output_path = Path(args.output) if args.output else None
    secrets: list[str] = (
        [config.api_key.get_secret_value()] if config.api_key is not None else []
    )

    try:
        result = run_loop(
            str(args.task),
            adapter=adapter,
            registry=registry,
            budget_config=config.budget,
            trajectory_path=output_path,
            secrets_to_redact=secrets,
        )
    finally:
        adapter.close()

    summary = {
        "run_id": result.run_id,
        "exit_reason": result.exit_reason.value,
        "final_answer": result.final_answer,
        "model_calls": result.model_calls,
        "tool_calls": result.tool_calls,
        "wall_time_sec": round(result.wall_time_sec, 4),
        "trajectory_path": str(result.trajectory_path) if result.trajectory_path else None,
        "error_detail": result.error_detail,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result.exit_reason == ExitReason.COMPLETED else 1


def _handle_replay(jsonl_path: str) -> int:
    try:
        report = replay_trajectory(Path(jsonl_path))
    except TrajectoryCorruptedError as exc:
        print(f"Replay error: {exc}", file=sys.stderr)
        return 1

    print(format_replay_report(report))
    return 0


def run_cli(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments, execute the requested subcommand, and return an exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "verify-protocol":
        return _handle_verify_protocol(bool(args.check))
    if args.subcommand == "run":
        return _handle_run(args)
    if args.subcommand == "replay":
        return _handle_replay(str(args.jsonl_path))

    parser.print_help(sys.stderr)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for console scripts and `python -m miniloop.cli`."""
    code = run_cli(argv)
    if code != 0:
        sys.exit(code)
    return 0


if __name__ == "__main__":
    sys.exit(main())
