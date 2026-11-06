"""Tool registry, zero-blackbox JSON Schema argument validator, and execution guard."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from miniloop.types import ToolCall, ToolResult

ToolHandler = Callable[[dict[str, Any], float], str]


class ToolExecutionError(RuntimeError):
    """Controlled business error raised by a tool handler to be reported back to the model."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ToolDefinition:
    """Metadata, JSON Schema parameters contract, and handler for a tool."""

    name: str
    description: str
    parameters_schema: dict[str, Any]
    handler: ToolHandler

    def to_openai_tool(self) -> dict[str, Any]:
        """Convert this tool definition to the OpenAI chat completions tool format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }


def _matches_json_type(value: Any, expected_type: str) -> bool:
    """Check whether a Python value matches the expected JSON Schema primitive/container type."""
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "object":
        return isinstance(value, dict)
    return False


def validate_tool_arguments(schema: dict[str, Any], arguments: Any) -> str | None:
    """Validate tool call arguments against a JSON Schema object definition.

    Returns None when arguments are valid, or a descriptive error string on failure.
    """
    if not isinstance(arguments, dict):
        return f"Arguments must be a JSON object, got {type(arguments).__name__}"

    required = schema.get("required", [])
    if isinstance(required, (list, tuple)):
        for req_field in required:
            if req_field not in arguments or arguments[req_field] is None:
                return f"Missing required argument '{req_field}'"

    raw_properties = schema.get("properties", {})
    properties: dict[str, Any] = raw_properties if isinstance(raw_properties, dict) else {}

    if schema.get("additionalProperties") is False:
        for key in arguments:
            if key not in properties:
                return f"Unknown argument '{key}' is not allowed"

    for key, value in arguments.items():
        if key not in properties:
            continue
        prop_schema = properties[key]
        if not isinstance(prop_schema, dict):
            continue
        expected_type = prop_schema.get("type")
        if expected_type is None:
            continue
        if not isinstance(expected_type, str) or not _matches_json_type(value, expected_type):
            return (
                f"Invalid type for argument '{key}': expected '{expected_type}', "
                f"got {type(value).__name__}"
            )

    return None


class ToolRegistry:
    """Registry managing tool definitions, pre-execution validation, and bounded execution."""

    def __init__(self, tools: Iterable[ToolDefinition] = ()) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: ToolDefinition) -> None:
        """Register a tool definition. Raises ValueError if the tool name is already registered."""
        if not tool.name or not tool.name.strip():
            raise ValueError("Tool name must not be empty")
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        """Look up a registered tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[ToolDefinition]:
        """Return all registered tool definitions in registration order."""
        return list(self._tools.values())

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """Serialize all registered tools into OpenAI function tool payloads."""
        return [tool.to_openai_tool() for tool in self._tools.values()]

    def execute(self, call: ToolCall, *, timeout_sec: float = 15.0) -> ToolResult:
        """Validate and execute a single tool call, returning a paired ToolResult."""
        start = time.monotonic()
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                content=f"Error [unknown_tool]: Unknown tool '{call.name}'",
                is_error=True,
                error_code="unknown_tool",
                duration_ms=(time.monotonic() - start) * 1000.0,
            )

        validation_error = validate_tool_arguments(tool.parameters_schema, call.arguments)
        if validation_error is not None:
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                content=f"Error [invalid_arguments]: {validation_error}",
                is_error=True,
                error_code="invalid_arguments",
                duration_ms=(time.monotonic() - start) * 1000.0,
            )

        result_box: list[str] = []
        error_box: list[BaseException] = []

        def _run_handler() -> None:
            try:
                result_box.append(tool.handler(dict(call.arguments), timeout_sec))
            except BaseException as exc:
                error_box.append(exc)

        worker = threading.Thread(target=_run_handler, daemon=True)
        try:
            worker.start()
            worker.join(timeout=timeout_sec)
            if worker.is_alive():
                raise TimeoutError
            if error_box:
                raise error_box[0]
            output = result_box[0]
        except TimeoutError:
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                content=(
                    f"Error [tool_timeout]: Tool '{call.name}' timed out after {timeout_sec:.2f}s"
                ),
                is_error=True,
                error_code="tool_timeout",
                duration_ms=(time.monotonic() - start) * 1000.0,
            )
        except ToolExecutionError as exc:
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                content=f"Error [{exc.code}]: {exc.message}",
                is_error=True,
                error_code=exc.code,
                duration_ms=(time.monotonic() - start) * 1000.0,
            )
        except Exception as exc:
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                content=f"Error [tool_runtime_error]: {exc}",
                is_error=True,
                error_code="tool_runtime_error",
                duration_ms=(time.monotonic() - start) * 1000.0,
            )

        return ToolResult(
            call_id=call.id,
            tool_name=call.name,
            content=output,
            is_error=False,
            error_code=None,
            duration_ms=(time.monotonic() - start) * 1000.0,
        )
