"""The agent loop (vendor-neutral).

    question -> planner proposes tool calls -> calls are validated and executed
             -> results go back to the planner -> more calls, or a final answer

Three responsibilities are kept apart:

* **planning** lives behind the :class:`~sci_evidence_agent.providers.Provider`
  interface (a language model, or the offline keyword planner);
* **tool execution** is :class:`~sci_evidence_agent.execution.ToolExecutor`
  (whitelist, argument validation, timing);
* **reporting** is the planner's final text: written by the model, or by
  :mod:`sci_evidence_agent.reporting` for the offline planner.

This module only moves messages between them. Constraints:

* Tools are executed by this program; the planner can only propose a call.
* At most ``MAX_TOOL_ROUNDS`` rounds, to stop a runaway loop.
* Every call is recorded with its arguments, duration and outcome.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sci_evidence_agent import storage
from sci_evidence_agent.execution import Execution, ToolExecutor, compact_view
from sci_evidence_agent.providers import Provider, get_provider
from sci_evidence_agent.schemas import Status, ToolCallLog, ToolResult
from sci_evidence_agent.tools import TOOL_SPECS

MAX_TOOL_ROUNDS = 5

SYSTEM_PROMPT = """You are a retrieval assistant for scientific databases. You can query \
PubChem, ChEMBL, UniProt, PubMed and LOTUS (natural products, via Wikidata).

Always call a tool when a question concerns specific database records; do not answer from \
memory. Even if you believe you know a compound's molecular formula or a protein's \
accession, look it up.

State facts only from tool results. Keep entity IDs, organisms, measurement types, values, \
units and source links.

How to present results:
- Found: group by database; give the ID and the source link for every record.
- Nothing found (no_match): say plainly that nothing was found. Do not rephrase it away and \
do not substitute an answer you remember.
- Request failed (error): say plainly that the request failed. That is not the same as \
"no relevant data".
- Several candidates (ambiguous): list the candidates and say that confirmation is needed. \
Do not choose for the user.
- Truncated: say that only the first N records were seen and how many exist in total.

Never fill in missing experimental data. If the units are null, say there are no units; if \
a value is missing, say it is missing.

When citing literature, give the PMID and, if available, the DOI link, both taken from \
tool results. Never write a title, author or year from memory: plausible-looking invented \
citations are the easiest error to make and the hardest to spot. If something cannot be \
found, say so.

Answer in the language of the user's question."""


@dataclass
class AgentRun:
    """Everything produced by one question; the UI and the evaluation consume it."""

    question: str
    session_id: str
    answer: str = ""
    results: list[ToolResult] = field(default_factory=list)
    call_log: list[ToolCallLog] = field(default_factory=list)
    executions: list[Execution] = field(default_factory=list)
    rounds: int = 0
    stopped_early: bool = False
    error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    provider_name: str = ""
    provider_model: str = ""
    is_real_model: bool = True

    def tool_rounds(self) -> list[list[str]]:
        """Tool names grouped by round, in call order."""
        grouped: dict[int, list[str]] = {}
        for execution in self.executions:
            grouped.setdefault(execution.round_index, []).append(execution.tool_name)
        return [grouped[i] for i in sorted(grouped)]


def ask(
    question: str,
    session_id: str | None = None,
    provider: Provider | str | None = None,
    executor: ToolExecutor | None = None,
) -> AgentRun:
    """Answer one question.

    Args:
        question: The user's question.
        session_id: Identifier for the call log; generated when omitted.
        provider: A Provider instance, a provider name, or None (chosen from
            the environment, falling back to the offline planner).
        executor: Tool executor; the default runs the whitelisted connectors.
    """
    session_id = session_id or uuid.uuid4().hex[:12]
    run = AgentRun(question, session_id)
    executor = executor or ToolExecutor()

    if not isinstance(provider, Provider):
        provider = get_provider(provider)

    run.provider_name = provider.name
    run.provider_model = provider.model
    run.is_real_model = provider.is_model

    try:
        ok, detail = provider.available()
        if not ok:
            run.error = f"{provider.name} is unavailable: {detail}"
            return run

        messages: list[Any] = [provider.user_message(question)]

        for round_index in range(MAX_TOOL_ROUNDS):
            run.rounds += 1
            response = provider.complete(SYSTEM_PROMPT, messages, TOOL_SPECS)

            for key, value in response.usage.items():
                run.usage[key] = run.usage.get(key, 0) + value

            if response.stop_reason == "refusal":
                run.error = response.note or "the model declined the request"
                return run

            # No tool call means the planner has written its final answer.
            if not response.tool_calls:
                run.answer = response.text
                break

            provider.append_assistant(messages, response)

            payloads: list[tuple[Any, str, bool]] = []
            for call in response.tool_calls:
                execution = executor.execute(call.name, call.arguments, session_id, round_index)
                result = execution.result
                run.executions.append(execution)
                run.results.append(result)
                run.call_log.append(
                    ToolCallLog(
                        tool_name=call.name,
                        arguments=call.arguments,
                        status=result.status,
                        duration_ms=execution.duration_ms,
                        from_cache=result.from_cache,
                        record_count=len(result.records),
                        error=result.error,
                    )
                )
                payloads.append((call, compact_view(result), result.status is Status.ERROR))

            provider.append_tool_results(messages, payloads)
        else:
            # The loop ran out of rounds without a final answer.
            run.stopped_early = True
            run.answer = (
                f"The limit of {MAX_TOOL_ROUNDS} tool rounds was reached before an answer "
                "was written. The data retrieved so far are listed below."
            )

    except Exception as exc:
        run.error = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        storage.log_question(session_id, question, run.answer or run.error)

    return run
