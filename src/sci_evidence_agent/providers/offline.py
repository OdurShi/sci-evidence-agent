"""Offline provider: plans tool calls by keyword rules instead of calling a model.

**This is not a model.** It wires the rule-based planner
(:mod:`sci_evidence_agent.planning`) and the template reporter
(:mod:`sci_evidence_agent.reporting`) into the provider interface, so the
agent loop runs unchanged without any API key. The UI and the report text
state clearly that the answer is not model output.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sci_evidence_agent.planning import Plan, Step, plan_question
from sci_evidence_agent.providers.base import LLMResponse, Provider, ToolCall, ToolSpec
from sci_evidence_agent.reporting import render_report


class OfflineProvider(Provider):
    name = "Offline planner (not a model)"
    model = "keyword-planner"
    is_model = False

    def __init__(self) -> None:
        self._plan: Plan | None = None
        self._round = 0
        # Tool results received so far: used to fill chained arguments and for the report.
        self._observations: list[tuple[str, dict[str, Any]]] = []
        self._skipped: list[str] = []

    @property
    def plan(self) -> Plan | None:
        """The plan built for the current question (None before the first turn)."""
        return self._plan

    def available(self) -> tuple[bool, str]:
        return True, "ready · keyword planning, no model call"

    def _resolve(self, step: Step) -> dict[str, Any] | None:
        """Arguments for a step, or None when its binding cannot be resolved.

        A chained value is only taken from a result with status ``ok``. An
        ambiguous lookup stops the chain: the candidates are reported and
        nobody picks one on the user's behalf.
        """
        args = dict(step.arguments)
        binding = step.binding
        if binding is None:
            return args
        for tool, payload in reversed(self._observations):
            if tool != binding.from_tool:
                continue
            status = payload.get("status")
            records = payload.get("records") or []
            value = records[0].get(binding.record_field) if records else None
            if status == "ok" and value:
                args[binding.argument] = value
                return args
            self._skipped.append(
                f"`{step.tool}` was not called because `{binding.from_tool}` returned "
                f"status {status} and gave no single {binding.argument} to continue with."
            )
            return None
        self._skipped.append(
            f"`{step.tool}` was not called because `{binding.from_tool}` produced no result."
        )
        return None

    def complete(self, system: str, messages: list[Any], tools: list[ToolSpec]) -> LLMResponse:
        available = {t.name for t in tools}

        if self._plan is None:
            question = ""
            for m in messages:
                if m.get("role") == "user" and isinstance(m.get("content"), str):
                    question = m["content"]
                    break
            self._plan = plan_question(question)

        # Issue the next round that still has something to call.
        while self._round < len(self._plan.rounds):
            steps = self._plan.rounds[self._round]
            self._round += 1

            calls: list[ToolCall] = []
            for step in steps:
                if step.tool not in available:
                    self._skipped.append(f"`{step.tool}` is not among the offered tools.")
                    continue
                args = self._resolve(step)
                if args is None:
                    continue
                calls.append(
                    ToolCall(id=f"offline_{uuid.uuid4().hex[:8]}", name=step.tool, arguments=args)
                )

            if calls:
                return LLMResponse(tool_calls=calls, stop_reason="tool_use", usage={})

        # All rounds done: write the report.
        text = render_report(self._plan, self._observations, self._skipped)
        return LLMResponse(text=text, stop_reason="end_turn", usage={})

    def append_assistant(self, messages: list[Any], response: LLMResponse) -> None:
        messages.append(
            {"role": "assistant", "content": response.text, "_calls": response.tool_calls}
        )

    def append_tool_results(
        self, messages: list[Any], results: list[tuple[ToolCall, str, bool]]
    ) -> None:
        for call, payload, _is_error in results:
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                parsed = {"summary": payload}
            self._observations.append((call.name, parsed))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": payload})
