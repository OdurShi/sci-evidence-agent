"""Agent loop with the offline planner and with scripted providers. HTTP is mocked."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sci_evidence_agent import agent
from sci_evidence_agent.agent import MAX_TOOL_ROUNDS, ask
from sci_evidence_agent.execution import ToolExecutor, compact_view, validate_arguments
from sci_evidence_agent.providers import (
    LLMResponse,
    Provider,
    ToolCall,
    get_provider,
    list_providers,
)
from sci_evidence_agent.providers.offline import OfflineProvider
from sci_evidence_agent.reporting import STATE_TEXT
from sci_evidence_agent.schemas import Status
from sci_evidence_agent.tools import DEFAULTS, REGISTRY, SPEC_BY_NAME, TOOL_SPECS


class ScriptedProvider(Provider):
    """Plays back a fixed list of responses, like a model would produce them."""

    name = "Scripted"
    model = "scripted-1"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.seen_tool_payloads: list[dict[str, Any]] = []
        self.system_prompts: list[str] = []

    def available(self) -> tuple[bool, str]:
        return True, "ready"

    def complete(self, system, messages, tools) -> LLMResponse:
        self.system_prompts.append(system)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def append_assistant(self, messages, response) -> None:
        messages.append({"role": "assistant", "content": response.text})

    def append_tool_results(self, messages, results) -> None:
        for call, payload, is_error in results:
            self.seen_tool_payloads.append({**json.loads(payload), "_is_error": is_error})
            messages.append({"role": "tool", "tool_call_id": call.id, "content": payload})


def call(name: str, **arguments: Any) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall("c1", name, arguments)], stop_reason="tool_use")


# -- offline planner, end to end ----------------------------------------------------


def test_single_source_question(upstream, store):
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    run = ask("What is the molecular formula of aspirin?", provider="offline")

    assert run.error is None and run.stopped_early is False
    assert run.is_real_model is False and run.provider_model == "keyword-planner"
    assert run.tool_rounds() == [["search_compound"]]
    assert run.rounds == 2  # one tool round, one answer round
    assert "`CID 2244`" in run.answer and "C9H8O4" in run.answer
    assert "not model output" in run.answer
    assert run.results[0].status is Status.OK

    log = run.call_log[0]
    assert (log.tool_name, log.status, log.record_count) == ("search_compound", Status.OK, 1)
    assert log.duration_ms == run.executions[0].duration_ms >= 0
    assert store.recent_calls(run.session_id)[0]["tool_name"] == "search_compound"


def test_two_round_chain_passes_the_chembl_id(upstream):
    upstream.add("molecule/search.json", "chembl/molecule_search_aspirin")
    upstream.add("activity.json", "chembl/activity_CHEMBL25")
    run = ask("List bioactivity assay records for aspirin from ChEMBL.", provider="offline")

    assert run.tool_rounds() == [["search_chembl_molecule"], ["get_bioactivities"]]
    assert run.rounds == 3
    assert run.executions[1].arguments == {"limit": 10, "chembl_id": "CHEMBL25"}
    assert [e.round_index for e in run.executions] == [0, 1]
    assert "molecule_chembl_id=CHEMBL25" in upstream.urls[1]
    assert "target CHEMBL" in run.answer and "(no units)" in run.answer


def test_chain_stops_on_an_ambiguous_lookup_and_lists_the_candidates(upstream):
    upstream.add("molecule/search.json", "chembl/molecule_search_paracetamol")
    run = ask("Show ChEMBL bioactivity data for paracetamol.", provider="offline")

    assert run.tool_rounds() == [["search_chembl_molecule"]]  # no second step
    assert run.results[0].status is Status.AMBIGUOUS
    assert STATE_TEXT["ambiguous"] in run.answer
    assert "`CHEMBL112`" in run.answer
    assert "`get_bioactivities` was not called" in run.answer
    assert upstream.count("activity.json") == 0


def test_chain_stops_on_no_match(upstream):
    upstream.add("molecule/search.json", "chembl/molecule_search_none")
    run = ask("Show ChEMBL bioactivity assay data for zzzznotacompound.", provider="offline")
    assert run.tool_rounds() == [["search_chembl_molecule"]]
    assert STATE_TEXT["no_match"] in run.answer
    assert STATE_TEXT["error"] not in run.answer


def test_cross_source_round_and_partial_failure(upstream):
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    upstream.add("uniprotkb/search", "uniprot/search_bad_field")
    run = ask(
        "Get the PubChem compound record for aspirin and search UniProt for the protein "
        '"kinase: human".',
        provider="offline",
    )
    assert run.tool_rounds() == [["search_compound", "search_protein"]]
    assert [r.status for r in run.results] == [Status.OK, Status.ERROR]
    assert run.error is None  # one failing tool does not fail the run
    assert STATE_TEXT["error"] in run.answer and "not a valid search field" in run.answer
    assert "`CID 2244`" in run.answer  # the other result is still reported


def test_no_recognised_entity_makes_no_call(upstream):
    run = ask("阿司匹林的分子式是什么？", provider="offline")
    assert run.executions == [] and run.rounds == 1
    assert "No entity to look up was recognised" in run.answer
    assert upstream.requests == []


def test_each_question_gets_a_fresh_offline_provider(upstream):
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    upstream.add("/compound/name/zzzznotacompound/", "pubchem/name_not_found")
    first = ask("formula of aspirin", provider="offline")
    second = ask("formula of zzzznotacompound", provider="offline")
    assert "CID 2244" in first.answer and "CID 2244" not in second.answer


def test_question_and_answer_are_logged(upstream, store):
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    run = ask("formula of aspirin", session_id="sess-1", provider="offline")
    import sqlite3

    conn = sqlite3.connect(store.db_path)
    try:
        row = conn.execute("SELECT session_id, question, answer FROM query_log").fetchone()
    finally:
        conn.close()
    assert row == ("sess-1", "formula of aspirin", run.answer)


# -- execution: whitelist and argument validation -------------------------------------


def test_unknown_tool_is_rejected_and_reported_to_the_planner(upstream):
    provider = ScriptedProvider([call("delete_database", target="all"), LLMResponse(text="done")])
    run = ask("anything", provider=provider)

    assert run.results[0].status is Status.ERROR
    assert "not whitelisted" in run.results[0].error
    assert provider.seen_tool_payloads[0]["_is_error"] is True
    assert upstream.requests == []
    assert run.answer == "done"


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        ({"query": 2244, "limit": 5}, "argument 'query' must be string, got int"),
        ({"query": "aspirin", "limit": "5"}, "argument 'limit' must be integer or null, got str"),
        ({"query": "aspirin", "limit": True}, "argument 'limit' must be integer or null"),
        ({"query": "aspirin", "limit": 5, "format": "json"}, "unexpected argument 'format'"),
        ({"limit": 5}, "missing required argument 'query'"),
        ({"query": "aspirin", "session_id": "x"}, "unexpected argument 'session_id'"),
    ],
)
def test_arguments_are_validated_against_the_schema(upstream, arguments, problem):
    result = ToolExecutor().run("search_compound", arguments, "s")
    assert result.status is Status.ERROR
    assert "arguments do not match the tool schema" in result.error and problem in result.error
    assert upstream.requests == []


def test_enum_is_enforced():
    spec = SPEC_BY_NAME["search_literature"]
    bad = {"term": "x", "limit": 5, "sort": "newest"}
    assert "must be one of ['relevance', 'pub_date']" in validate_arguments(spec, bad)[0]
    assert validate_arguments(spec, {"term": "x", "limit": None, "sort": "pub_date"}) == []


def test_null_and_missing_optional_arguments_get_defaults(upstream):
    upstream.add("uniprotkb/search", "uniprot/search_egfr_human")
    executor = ToolExecutor()
    full = {"query": "gene:EGFR", "organism_id": None, "reviewed_only": None, "limit": None}
    result = executor.run("search_protein", full, "s")
    assert result.status is Status.OK
    assert result.query == {
        "query": "gene:EGFR",
        "organism_id": None,
        "reviewed_only": True,
        "limit": 10,
    }
    # A provider without strict mode may simply leave optional arguments out.
    assert executor.run("search_protein", {"query": "gene:EGFR"}, "s").from_cache is True


def test_unparsable_model_arguments_become_an_error_result():
    result = ToolExecutor().run("search_compound", {"__parse_error__": "{not json"}, "s")
    assert result.status is Status.ERROR and "not valid JSON" in result.error


def test_tool_exception_becomes_an_error_result():
    def broken(**_kwargs):
        raise RuntimeError("boom")

    executor = ToolExecutor(registry={"broken": broken}, specs={}, defaults={})
    execution = executor.execute("broken", {}, "s", round_index=2)
    assert (
        execution.result.status is Status.ERROR and "RuntimeError: boom" in execution.result.error
    )
    assert execution.round_index == 2 and execution.duration_ms >= 0


def test_tool_specs_registry_and_defaults_agree():
    names = [spec.name for spec in TOOL_SPECS]
    assert sorted(names) == sorted(REGISTRY) == sorted(DEFAULTS)
    assert len(names) == 8
    for spec in TOOL_SPECS:
        schema = spec.parameters
        # Strict tool use needs every property required and no extra properties.
        assert schema["additionalProperties"] is False
        assert schema["required"] == list(schema["properties"])
        for prop in schema["properties"].values():
            # An enum on a union type is rejected by strict mode.
            assert not ("enum" in prop and isinstance(prop["type"], list))
        assert set(DEFAULTS[spec.name]) <= set(schema["properties"])


def test_compact_view_drops_empty_fields(upstream):
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    result = ToolExecutor().run("search_compound", {"query": "aspirin", "limit": 5}, "s")
    view = json.loads(compact_view(result))
    assert view["status"] == "ok" and view["summary"] == "PubChem: 1 record"
    assert "truncated" not in view and "error" not in view and "warnings" not in view
    assert all(v not in (None, [], "") for v in view["records"][0].values())


# -- loop control -------------------------------------------------------------------


def test_loop_stops_after_the_round_limit(upstream):
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    provider = ScriptedProvider([call("search_compound", query="aspirin", limit=5)])
    run = ask("loop forever", provider=provider)

    assert run.stopped_early is True
    assert run.rounds == MAX_TOOL_ROUNDS == 5
    assert len(run.executions) == 5
    assert "limit of 5 tool rounds" in run.answer
    assert len(upstream.requests) == 1  # the other four were cache hits


def test_refusal_ends_the_run_with_an_error():
    provider = ScriptedProvider([LLMResponse(stop_reason="refusal", note="declined (cyber)")])
    run = ask("x", provider=provider)
    assert run.error == "declined (cyber)" and run.answer == "" and run.executions == []


def test_unavailable_provider_is_reported(monkeypatch):
    run = ask("x", provider="anthropic")  # no key in the test environment
    assert run.error == "Anthropic is unavailable: ANTHROPIC_API_KEY is not set"


def test_provider_exception_does_not_crash_the_caller(upstream):
    class Exploding(ScriptedProvider):
        def complete(self, system, messages, tools):
            raise ConnectionError("model endpoint unreachable")

    run = ask("x", provider=Exploding([]))
    assert run.error == "ConnectionError: model endpoint unreachable"


def test_usage_is_summed_over_rounds(upstream):
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    first = call("search_compound", query="aspirin", limit=None)
    first.usage = {"input_tokens": 100, "output_tokens": 20}
    last = LLMResponse(
        text="Aspirin is CID 2244.", usage={"input_tokens": 150, "output_tokens": 30}
    )
    run = ask("x", provider=ScriptedProvider([first, last]))
    assert run.usage == {"input_tokens": 250, "output_tokens": 50}
    assert run.is_real_model is True and run.answer == "Aspirin is CID 2244."


def test_system_prompt_states_the_ground_rules():
    prompt = agent.SYSTEM_PROMPT
    for source in ("PubChem", "ChEMBL", "UniProt", "PubMed", "LOTUS"):
        assert source in prompt
    for state in ("no_match", "error", "ambiguous"):
        assert state in prompt
    assert "do not answer from memory" in prompt


# -- provider registry ----------------------------------------------------------------


def test_provider_selection_falls_back_to_offline_without_keys(monkeypatch):
    assert isinstance(get_provider(), OfflineProvider)  # anthropic is the default, no key set
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    assert isinstance(get_provider(), OfflineProvider)
    assert get_provider("anthropic").name == "Anthropic"  # an explicit name is honoured
    with pytest.raises(ValueError, match="unknown provider"):
        get_provider("nope")


def test_list_providers_reports_availability():
    listed = {key: (ok, detail) for key, _name, ok, detail in list_providers()}
    assert set(listed) == {"anthropic", "openai", "offline"}
    assert listed["offline"] == (True, "ready · keyword planning, no model call")
    assert listed["anthropic"][0] is False
