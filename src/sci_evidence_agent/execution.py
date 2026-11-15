"""Tool execution: whitelist check, argument validation, timing, compact view.

The model (or the offline planner) only *proposes* calls. This module decides
whether a proposal is executed, runs it, measures it, and builds the reduced
JSON view that goes back to the planner. It knows nothing about providers or
about how the final answer is written.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sci_evidence_agent import tools as tool_registry
from sci_evidence_agent.providers.base import ToolSpec
from sci_evidence_agent.schemas import Status, ToolResult

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
    "object": (dict,),
    "array": (list,),
}


def _matches_type(value: Any, json_type: str) -> bool:
    if json_type in ("integer", "number") and isinstance(value, bool):
        return False  # bool is a subclass of int in Python, not in JSON Schema
    return isinstance(value, _JSON_TYPES.get(json_type, (object,)))


def validate_arguments(
    spec: ToolSpec, args: dict[str, Any], defaults: dict[str, Any] | None = None
) -> list[str]:
    """Check arguments against a tool's JSON Schema; return a list of problems.

    Covers what the tool schemas use: unknown keys, missing required keys,
    primitive types (including ``["type", "null"]`` unions) and ``enum``.
    A missing key is accepted when a default exists for it, because providers
    without a strict mode often omit optional arguments instead of sending null.
    """
    problems: list[str] = []
    properties: dict[str, Any] = spec.parameters.get("properties", {})
    defaults = defaults or {}

    for key in args:
        if key not in properties:
            problems.append(f"unexpected argument {key!r}")

    for key, schema in properties.items():
        if key not in args:
            if key in spec.parameters.get("required", []) and key not in defaults:
                problems.append(f"missing required argument {key!r}")
            continue
        value = args[key]
        declared = schema.get("type")
        allowed = declared if isinstance(declared, list) else [declared] if declared else []
        if allowed and not any(_matches_type(value, t) for t in allowed):
            problems.append(
                f"argument {key!r} must be {' or '.join(allowed)}, got {type(value).__name__}"
            )
            continue
        if "enum" in schema and value is not None and value not in schema["enum"]:
            problems.append(f"argument {key!r} must be one of {schema['enum']}, got {value!r}")
    return problems


@dataclass
class Execution:
    """One executed (or rejected) tool call."""

    tool_name: str
    arguments: dict[str, Any]  # as proposed by the planner
    result: ToolResult
    duration_ms: int
    round_index: int = 0


class ToolExecutor:
    """Runs whitelisted tools with validated arguments."""

    def __init__(
        self,
        registry: dict[str, Callable[..., ToolResult]] | None = None,
        specs: dict[str, ToolSpec] | None = None,
        defaults: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.registry = tool_registry.REGISTRY if registry is None else registry
        self.specs = tool_registry.SPEC_BY_NAME if specs is None else specs
        self.defaults = tool_registry.DEFAULTS if defaults is None else defaults

    def _rejected(self, raw_args: dict[str, Any], message: str) -> ToolResult:
        return ToolResult(status=Status.ERROR, source="unknown", query=raw_args, error=message)

    def run(self, name: str, raw_args: dict[str, Any], session_id: str | None) -> ToolResult:
        """Validate and execute one call. Never raises; problems become error results."""
        if name not in self.registry:
            # The model invented a tool. Say so; never pretend it was queried.
            return self._rejected(raw_args, f"tool {name!r} is not whitelisted; not executed")

        # OpenAI-compatible endpoints give no strictness guarantee: arguments may not parse.
        if "__parse_error__" in raw_args:
            snippet = str(raw_args["__parse_error__"])[:120]
            return self._rejected(raw_args, f"the model's arguments are not valid JSON: {snippet}")

        spec = self.specs.get(name)
        defaults = self.defaults.get(name, {})
        if spec is not None:
            problems = validate_arguments(spec, raw_args, defaults)
            if problems:
                return self._rejected(
                    raw_args, "arguments do not match the tool schema: " + "; ".join(problems)
                )

        # Replace nulls (and missing optional arguments) with defaults.
        args = {k: v for k, v in raw_args.items() if v is not None}
        for key, default in defaults.items():
            args.setdefault(key, default)

        try:
            return self.registry[name](**args, session_id=session_id)
        except TypeError as exc:
            # Only reachable for tools registered without a schema.
            return self._rejected(args, f"arguments do not match the tool signature: {exc}")
        except Exception as exc:
            return self._rejected(args, f"{type(exc).__name__}: {exc}")

    def execute(
        self,
        name: str,
        raw_args: dict[str, Any],
        session_id: str | None,
        round_index: int = 0,
    ) -> Execution:
        """Run one call and measure its wall-clock duration."""
        started = time.monotonic()
        result = self.run(name, raw_args, session_id)
        duration_ms = int((time.monotonic() - started) * 1000)
        return Execution(name, raw_args, result, duration_ms, round_index)


def compact_view(result: ToolResult) -> str:
    """Reduced JSON view of a result for the planner.

    Context is a limited resource: only non-empty fields are sent back. The
    complete records stay in the ToolResult for the UI and for provenance.
    """
    payload: dict[str, Any] = {
        "status": result.status.value,
        "source": result.source,
        "summary": result.summary(),
        "source_url": result.source_url,
        "retrieved_at": result.retrieved_at,
        "from_cache": result.from_cache,
    }
    if result.truncated:
        payload["truncated"] = True
        payload["total_available"] = result.total_available
    if result.warnings:
        payload["warnings"] = result.warnings
    if result.error:
        payload["error"] = result.error
    if result.records:
        payload["records"] = [
            {k: v for k, v in rec.items() if v not in (None, [], "")} for rec in result.records
        ]
    return json.dumps(payload, ensure_ascii=False)
