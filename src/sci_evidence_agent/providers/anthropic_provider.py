"""Anthropic (Claude) provider.

Points that matter:

* Tool definitions use ``input_schema`` with ``strict`` enabled, so arguments
  are guaranteed to match the schema.
* ``response.content`` is passed back unchanged; it can contain thinking
  blocks that must not be dropped or reordered.
* A refusal is detected from ``stop_reason`` before the content is read.
* ``fallbacks`` is a server-side fallback: when a safety classifier declines a
  request, the API reroutes it by refusal category.

This provider was carried over from the prototype without being exercised in
this repository's tests or evaluation (no API key is used); see
``docs/notes.md``.
"""

from __future__ import annotations

import os
from typing import Any

from sci_evidence_agent.providers.base import LLMResponse, Provider, ToolCall, ToolSpec

DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOKENS = 16000


class AnthropicProvider(Provider):
    name = "Anthropic"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
        self._client: Any = None

    def available(self) -> tuple[bool, str]:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False, "ANTHROPIC_API_KEY is not set"
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return (
                False,
                "the anthropic package is not installed; pip install 'sci-evidence-agent[anthropic]'",  # noqa: E501,
            )
        return True, f"ready · {self.model}"

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def _tools(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        # ``strict`` needs every property listed in ``required`` and
        # ``additionalProperties: false``; the ToolSpec definitions guarantee
        # that, so only field names are translated here.
        return [
            {
                "name": t.name,
                "description": t.description,
                "strict": True,
                "input_schema": t.parameters,
            }
            for t in tools
        ]

    def complete(self, system: str, messages: list[Any], tools: list[ToolSpec]) -> LLMResponse:
        client = self._get_client()

        response = client.beta.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=self._tools(tools),
            messages=messages,
            output_config={"effort": "medium"},
            # Reroute by refusal category when a safety classifier declines.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }

        # A refusal must be checked before reading the content.
        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            category = getattr(detail, "category", None) or "unknown"
            return LLMResponse(
                stop_reason="refusal",
                usage=usage,
                note=f"the model declined the request (category: {category})",
                raw=response,
            )

        calls = [
            ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
            for b in response.content
            if b.type == "tool_use"
        ]
        text = "".join(b.text for b in response.content if b.type == "text")

        return LLMResponse(
            text=text,
            tool_calls=calls,
            stop_reason="tool_use" if calls else (response.stop_reason or "end_turn"),
            usage=usage,
            raw=response,
        )

    def append_assistant(self, messages: list[Any], response: LLMResponse) -> None:
        # Pass the content back as received; thinking blocks must travel with it.
        messages.append({"role": "assistant", "content": response.raw.content})

    def append_tool_results(
        self, messages: list[Any], results: list[tuple[ToolCall, str, bool]]
    ) -> None:
        # All results of a round go into one user message; splitting them
        # teaches the model to stop issuing parallel calls.
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": payload,
                        "is_error": is_error,
                    }
                    for call, payload, is_error in results
                ],
            }
        )
