"""Registry of model providers.

Select one with the ``LLM_PROVIDER`` environment variable or in the UI:

    LLM_PROVIDER=anthropic   needs ANTHROPIC_API_KEY (default)
    LLM_PROVIDER=openai      OpenAI-compatible endpoint: LLM_BASE_URL / LLM_MODEL / LLM_API_KEY
    LLM_PROVIDER=offline     keyword planner, no model and no key

``offline`` is the automatic fallback: when no provider is named explicitly
and the configured one is unavailable, the pipeline still runs.
"""

from __future__ import annotations

import os

from sci_evidence_agent.providers.anthropic_provider import AnthropicProvider
from sci_evidence_agent.providers.base import LLMResponse, Provider, ToolCall, ToolSpec
from sci_evidence_agent.providers.offline import OfflineProvider
from sci_evidence_agent.providers.openai_compat import OpenAICompatProvider

__all__ = [
    "PROVIDERS",
    "LLMResponse",
    "Provider",
    "ToolCall",
    "ToolSpec",
    "get_provider",
    "list_providers",
]

PROVIDERS: dict[str, type[Provider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAICompatProvider,
    "offline": OfflineProvider,
}


def get_provider(name: str | None = None) -> Provider:
    """Return a new provider instance (the offline provider keeps per-question state).

    Without a name, ``LLM_PROVIDER`` is read and defaults to ``anthropic``;
    if that provider is unavailable the offline planner is returned instead.
    A provider requested by name is returned as is, available or not.
    """
    key = (name or os.environ.get("LLM_PROVIDER") or "anthropic").strip().lower()
    cls = PROVIDERS.get(key)
    if cls is None:
        raise ValueError(f"unknown provider {key!r}; choose from: {', '.join(PROVIDERS)}")

    provider = cls()
    if name is None and key != "offline":
        ok, _ = provider.available()
        if not ok:
            return OfflineProvider()
    return provider


def list_providers() -> list[tuple[str, str, bool, str]]:
    """All providers with their availability: ``(key, display name, ok, detail)``."""
    out = []
    for key, cls in PROVIDERS.items():
        instance = cls()
        ok, detail = instance.available()
        out.append((key, instance.name, ok, detail))
    return out
