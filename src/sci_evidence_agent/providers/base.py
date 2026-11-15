"""Vendor-neutral interface for model providers.

Vendors differ in three places, and all three are differences of format, not
of concept:

1. the field names of a tool definition (Anthropic: ``input_schema``; OpenAI:
   ``parameters`` inside a ``function`` wrapper);
2. where tool calls sit in a response (Anthropic: ``tool_use`` content blocks;
   OpenAI: ``message.tool_calls``);
3. the shape of the message history, in particular how tool results are sent
   back.

This module therefore defines neutral ``ToolSpec`` / ``ToolCall`` /
``LLMResponse`` types and leaves "how to append to the history" to each
provider. The agent loop never looks inside the message list.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    """Neutral tool definition; each provider translates it to its own format."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass(frozen=True)
class ToolCall:
    """One call proposed by the model. ``id`` pairs the result with the call."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """Neutral response of one model turn."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # end_turn | tool_use | refusal | max_tokens | error
    stop_reason: str = "end_turn"
    usage: dict[str, int] = field(default_factory=dict)
    note: str | None = None  # vendor-specific remark, e.g. a refusal category
    raw: Any = None  # for the provider's own use; the agent never touches it


class Provider(abc.ABC):
    """Interface implemented by every provider.

    The two ``append_*`` methods exist because the history format is where
    vendors differ most; the agent only ever holds an opaque list.
    """

    #: Display name.
    name: str = "unknown"
    #: Identifier of the model in use.
    model: str = ""
    #: False for the offline keyword planner, whose text is not model output.
    is_model: bool = True

    @abc.abstractmethod
    def available(self) -> tuple[bool, str]:
        """Whether the provider can be used: ``(ok, human-readable detail)``."""

    @abc.abstractmethod
    def complete(
        self,
        system: str,
        messages: list[Any],
        tools: list[ToolSpec],
    ) -> LLMResponse:
        """Send one turn."""

    @abc.abstractmethod
    def append_assistant(self, messages: list[Any], response: LLMResponse) -> None:
        """Append the model's turn to the history.

        Anthropic requires the content to be passed back unchanged (including
        thinking blocks), so providers use ``response.raw`` rather than
        rebuilding the message.
        """

    @abc.abstractmethod
    def append_tool_results(
        self,
        messages: list[Any],
        results: list[tuple[ToolCall, str, bool]],
    ) -> None:
        """Append tool results to the history.

        Args:
            messages: The opaque history list.
            results: ``(call, result as JSON string, is_error)`` triples. All
                results of one round must be sent back together.
        """

    def user_message(self, text: str) -> Any:
        """Build a user message. Identical across vendors so far."""
        return {"role": "user", "content": text}
