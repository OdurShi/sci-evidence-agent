"""Provider for OpenAI-compatible chat-completions endpoints.

One class covers every service that implements ``/v1/chat/completions``:
OpenAI itself, hosted models with a compatible API (DeepSeek, Qwen, Kimi,
GLM, ...), and local servers (vLLM, Ollama, LM Studio).

Switching service only changes environment variables::

    LLM_BASE_URL=https://api.deepseek.com/v1
    LLM_MODEL=deepseek-chat

    LLM_BASE_URL=http://localhost:11434/v1     # Ollama
    LLM_MODEL=qwen2.5:32b

The three differences from Anthropic are absorbed here:

1. tool definitions are wrapped as ``{"type": "function", "function": {...}}``
   and the schema field is called ``parameters``;
2. tool calls sit in ``message.tool_calls`` and their arguments are a JSON
   string, not an object;
3. each tool result is its own ``{"role": "tool", "tool_call_id": ...}``
   message.

This provider was carried over from the prototype, where it had never been
run against a live endpoint; it is not exercised here either. See
``docs/notes.md``.
"""

from __future__ import annotations

import json
import os
from typing import Any

from sci_evidence_agent.providers.base import LLMResponse, Provider, ToolCall, ToolSpec

DEFAULT_MODEL = "gpt-4o-mini"
MAX_TOKENS = 4096


class OpenAICompatProvider(Provider):
    name = "OpenAI-compatible"

    def __init__(self, model: str | None = None, base_url: str | None = None) -> None:
        self.model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
        self.base_url = base_url or os.environ.get("LLM_BASE_URL") or None
        self._client: Any = None

    def available(self) -> tuple[bool, str]:
        try:
            import openai  # noqa: F401
        except ImportError:
            return (
                False,
                "the openai package is not installed; pip install 'sci-evidence-agent[openai]'",
            )

        key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not key:
            # Local servers usually ignore the key, but the SDK needs a non-empty one.
            if self.base_url and ("localhost" in self.base_url or "127.0.0.1" in self.base_url):
                return True, f"ready · {self.model} (local, no key needed)"
            return False, "LLM_API_KEY or OPENAI_API_KEY is not set"

        where = f" @ {self.base_url}" if self.base_url else ""
        return True, f"ready · {self.model}{where}"

    def _get_client(self) -> Any:
        if self._client is None:
            import openai

            key = (
                os.environ.get("LLM_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
                or "not-needed-for-local"
            )
            self._client = openai.OpenAI(api_key=key, base_url=self.base_url)
        return self._client

    def _tools(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    def complete(self, system: str, messages: list[Any], tools: list[ToolSpec]) -> LLMResponse:
        client = self._get_client()

        response = client.chat.completions.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "system", "content": system}, *messages],
            tools=self._tools(tools),
            tool_choice="auto",
        )

        choice = response.choices[0]
        message = choice.message

        usage = {}
        if response.usage:
            usage = {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
            }

        calls: list[ToolCall] = []
        for tc in message.tool_calls or []:
            # Arguments are a JSON string without a strictness guarantee.
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"__parse_error__": tc.function.arguments}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

        return LLMResponse(
            text=message.content or "",
            tool_calls=calls,
            stop_reason="tool_use" if calls else (choice.finish_reason or "end_turn"),
            usage=usage,
            raw=message,
        )

    def append_assistant(self, messages: list[Any], response: LLMResponse) -> None:
        entry: dict[str, Any] = {"role": "assistant", "content": response.text or None}
        if response.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": c.name,
                        "arguments": json.dumps(c.arguments, ensure_ascii=False),
                    },
                }
                for c in response.tool_calls
            ]
        messages.append(entry)

    def append_tool_results(
        self, messages: list[Any], results: list[tuple[ToolCall, str, bool]]
    ) -> None:
        # Unlike Anthropic: one ``tool`` message per result.
        for call, payload, _is_error in results:
            messages.append({"role": "tool", "tool_call_id": call.id, "content": payload})
