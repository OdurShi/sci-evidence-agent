"""Evaluation harness: scoring, aggregation, and record/replay determinism."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from helpers import Upstream
from sci_evidence_agent import http as http_layer
from sci_evidence_agent import storage
from sci_evidence_agent.agent import ask
from sci_evidence_agent.cassette import Cassette, RecordingTransport, ReplayTransport
from sci_evidence_agent.evaluation import (
    FABRICATED,
    aggregate,
    load_benchmark,
    percentile,
    render_markdown,
    run_evaluation,
    score_planning_only,
    score_question,
    summarise,
)

MINI = {
    "name": "mini",
    "version": "0",
    "questions": [
        {
            "id": "M1",
            "category": "compound",
            "question": "What is the molecular formula of aspirin?",
            "expected_tools": [["search_compound"]],
            "expected_statuses": [["ok"]],
            "expected_identifiers": [
                {"kind": "cid", "value": "2244"},
                {"kind": "inchikey", "value": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"},
            ],
        },
        {
            "id": "M2",
            "category": "bioactivity_chain",
            "question": "Show ChEMBL bioactivity data for paracetamol.",
            "expected_tools": [["search_chembl_molecule"]],
            "expected_statuses": [["ambiguous"]],
            "expected_identifiers": [{"kind": "chembl", "value": "CHEMBL112"}],
        },
        {
            "id": "M3",
            "category": "protein",
            "question": 'Search UniProt for the protein "kinase: human".',
            "expected_tools": [["search_protein"]],
            "expected_statuses": [["error"]],
            "expected_identifiers": [],
        },
        {
            "id": "M4",
            "category": "literature",
            "question": "Fetch the PubMed record for PMID 14592543 with its DOI.",
            "expected_tools": [["get_article"]],
            "expected_statuses": [["ok"]],
            "expected_identifiers": [
                {"kind": "pmid", "value": "14592543"},
                {"kind": "doi", "value": "10.1016/S0049-3848(03)00379-7"},
            ],
        },
    ],
}


def add_mini_routes(upstream: Upstream) -> Upstream:
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    upstream.add("molecule/search.json", "chembl/molecule_search_paracetamol")
    upstream.add("uniprotkb/search", "uniprot/search_bad_field")
    upstream.add("esummary.fcgi", "pubmed/esummary_14592543")
    return upstream


def test_percentile_uses_linear_interpolation():
    assert percentile([], 50) is None
    assert percentile([3.0], 90) == 3.0
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([4, 1, 3, 2], 50) == 2.5  # order does not matter
    assert percentile([1, 2, 3, 4, 5], 90) == pytest.approx(4.6)
    assert percentile([1, 2, 3, 4, 5], 0) == 1 and percentile([1, 2, 3, 4, 5], 100) == 5


def test_benchmark_file_is_well_formed():
    root = Path(__file__).parent.parent / "benchmark"
    data = load_benchmark(root / "questions.json")
    questions = data["questions"]
    assert len(questions) == 34
    assert len({q["id"] for q in questions}) == 34
    counts: dict[str, int] = {}
    for q in questions:
        counts[q["category"]] = counts.get(q["category"], 0) + 1
        # Tools and statuses have the same round layout.
        assert [len(r) for r in q["expected_tools"]] == [len(r) for r in q["expected_statuses"]]
        assert all(
            s in {"ok", "no_match", "ambiguous", "error"} for r in q["expected_statuses"] for s in r
        )
        assert all(
            e["kind"] in {"cid", "chembl", "uniprot", "pmid", "doi", "inchikey"}
            for e in q["expected_identifiers"]
        )
    assert counts == {
        "compound": 6,
        "bioactivity_chain": 7,
        "protein": 7,
        "literature": 7,
        "cross_source": 7,
    }
    states = {s for q in questions for r in q["expected_statuses"] for s in r}
    assert states == {"ok", "no_match", "ambiguous", "error"}
    assert len(load_benchmark(root / "heldout_paraphrases.json")["questions"]) == 20


def test_load_benchmark_rejects_other_json(tmp_path):
    other = tmp_path / "x.json"
    other.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="not a benchmark file"):
        load_benchmark(other)


def test_score_question_on_a_correct_run(upstream, store):
    add_mini_routes(upstream)
    question = MINI["questions"][0]
    record = score_question(question, ask(question["question"], provider="offline"), 0.5, store)

    assert record["tool_selection_exact"] is True and record["status_match"] is True
    assert record["called_tools"] == [["search_compound"]]
    assert record["identifiers_found"] == ["cid:2244", "inchikey:BSYNRYMUTXBXSQ-UHFFFAOYSA-N"]
    assert record["verification"]["untraceable"] == 0
    assert record["verification"]["traceable"] == record["verification"]["identifiers"] == 2
    assert record["fabrication_detected"] is True
    assert record["handling"] is None  # nothing but ok was expected
    assert record["calls"][0]["raw_path"].startswith("raw/pubchem_")
    assert record["cache_hits"] == 0 and record["wall_s"] == 0.5


def test_score_question_flags_wrong_tools_and_missing_identifiers(upstream, store):
    add_mini_routes(upstream)
    wrong = {
        **MINI["questions"][0],
        "expected_tools": [["search_chembl_molecule"], ["get_bioactivities"]],
        "expected_statuses": [["ok"], ["ok"]],
        "expected_identifiers": [{"kind": "chembl", "value": "CHEMBL25"}],
    }
    record = score_question(wrong, ask(wrong["question"], provider="offline"), 0.1, store)
    assert record["tool_selection_exact"] is False and record["status_match"] is False
    assert (record["tool_overlap"], record["tools_called"], record["tools_expected"]) == (0, 1, 2)
    assert record["identifiers_found"] == []


def test_non_ok_states_are_scored_for_handling(upstream, store):
    add_mini_routes(upstream)
    ambiguous, error = MINI["questions"][1], MINI["questions"][2]
    r1 = score_question(ambiguous, ask(ambiguous["question"], provider="offline"), 0.1, store)
    r2 = score_question(error, ask(error["question"], provider="offline"), 0.1, store)
    assert r1["handling"] is True and r1["statuses"] == [["ambiguous"]]
    assert r2["handling"] is True and r2["statuses"] == [["error"]]
    # Had the run ended differently from what the benchmark expects, handling fails.
    r3 = score_question(
        {**error, "expected_statuses": [["no_match"]]},
        ask(error["question"], provider="offline"),
        0.1,
        store,
    )
    assert r3["handling"] is False and r3["status_match"] is False


def test_fabricated_probe_identifiers_cover_every_kind():
    assert sorted(i.kind for i in FABRICATED) == sorted(
        ["cid", "chembl", "uniprot", "pmid", "doi", "inchikey"]
    )


def test_aggregate_and_summarise(upstream, store):
    add_mini_routes(upstream)
    records = [
        score_question(q, ask(q["question"], provider="offline"), wall, store)
        for q, wall in zip(MINI["questions"], [0.2, 0.4, 0.6, 0.8], strict=True)
    ]
    overall = aggregate(records)
    assert overall["n"] == 4
    assert overall["tool_selection_exact"] == 1.0 and overall["status_match"] == 1.0
    assert (overall["identifiers_found"], overall["identifiers_expected"]) == (5, 5)
    assert overall["identifier_recall"] == 1.0
    assert overall["untraceable"] == 0 and overall["traceable_share"] == 1.0
    assert (overall["non_ok_handled"], overall["non_ok_questions"]) == (2, 2)
    assert overall["fabrication_detected"] == 1.0
    assert overall["latency_median_s"] == 0.5 and overall["latency_p90_s"] == 0.74
    assert overall["cache_hit_rate"] == 0.0 and overall["tool_calls"] == 4
    assert aggregate([]) == {"n": 0}

    summary = summarise(records)
    assert list(summary["per_category"]) == [
        "compound",
        "bioactivity_chain",
        "protein",
        "literature",
    ]
    assert summary["per_category"]["protein"]["identifier_recall"] is None  # nothing expected


def test_planning_only_scoring():
    result = score_planning_only(
        [
            {"id": "a", "category": "compound", "question": "formula of aspirin",
             "expected_tools": [["search_compound"]]},
            {"id": "b", "category": "compound", "question": "阿司匹林的分子式是什么？",
             "expected_tools": [["search_compound"]]},
        ]
    )  # fmt: skip
    assert (result["n"], result["exact"], result["accuracy"]) == (2, 1, 0.5)
    assert [q["exact"] for q in result["questions"]] == [True, False]
    assert result["per_category"] == {"compound": {"n": 2, "exact": 1, "arguments_exact": 0}}
    assert result["arguments_accuracy"] is None  # no expected_arguments were given


def test_planning_only_scoring_checks_arguments_too():
    # The right tool with the wrong query must not count as correct.
    questions = [
        {
            "id": "ok",
            "category": "compound",
            "question": "formula of aspirin",
            "expected_tools": [["search_compound"]],
            "expected_arguments": [{"tool": "search_compound", "arguments": {"query": "aspirin"}}],
        },
        {
            "id": "wrong-query",
            "category": "compound",
            "question": "I need the SMILES string for ibuprofen.",
            "expected_tools": [["search_compound"]],
            "expected_arguments": [
                {"tool": "search_compound", "arguments": {"query": "ibuprofen"}}
            ],
        },
    ]
    result = score_planning_only(questions)
    assert (result["exact"], result["arguments_exact"]) == (2, 1)
    assert (result["accuracy"], result["arguments_accuracy"]) == (1.0, 0.5)
    wrong = result["questions"][1]
    assert wrong["exact"] is True and wrong["arguments_exact"] is False
    assert wrong["planned_arguments"] == [[{"query": "string", "limit": 5}]]  # a known weakness


def test_full_evaluation_writes_outputs_and_second_pass_hits_the_cache(tmp_path, monkeypatch):
    bench = tmp_path / "mini.json"
    bench.write_text(json.dumps(MINI), encoding="utf-8")
    out = tmp_path / "out"

    # Serve the "live" pass from fixtures (no rate-limit waits) and record it.
    fake = add_mini_routes(Upstream())
    monkeypatch.setattr(http_layer, "default_intervals", lambda: {"PubChem": 0.0})
    monkeypatch.setattr(http_layer.RateLimiter, "interval", lambda self, bucket: 0.0)

    summary = run_evaluation(
        bench, out, record=True, echo=False, network=httpx.MockTransport(fake.handler)
    )

    cold, warm = summary["passes"]["pass1_cold"], summary["passes"]["pass2_warm"]
    assert cold["overall"]["tool_selection_exact"] == 1.0
    assert cold["overall"]["cache_hit_rate"] == 0.0
    assert summary["http_requests"] == {"pass1_cold": 4, "pass2_warm": 1}
    # The error result is never cached, so 3 of 4 calls are cache hits.
    assert (warm["overall"]["cache_hits"], warm["overall"]["tool_calls"]) == (3, 4)
    assert summary["answers_identical_across_passes"] == 4
    assert summary["mode"] == "live" and summary["environment"]["python"]

    for name in (
        "summary.json", "summary.md", "pass1_cold.jsonl", "pass2_warm.jsonl",
        "benchmark_snapshot.json", "environment.json", "call_log.jsonl", "http_cassette.json.gz",
    ):  # fmt: skip
        assert (out / name).is_file(), name
    assert not (out / "app.db").exists()  # the cache database is not part of the results
    assert len(list((out / "raw").glob("*.json"))) == 3  # no raw file for the error
    assert len((out / "call_log.jsonl").read_text().splitlines()) == 8
    assert "| overall | 4 |" in (out / "summary.md").read_text()
    assert storage._default is None  # the harness leaves no global state behind

    # Replaying the cassette reproduces every metric except timing.
    replayed = run_evaluation(bench, tmp_path / "replay", replay=out / "http_cassette.json.gz",
                              echo=False)  # fmt: skip
    assert replayed["mode"] == "replay"
    timing = {"latency_median_s", "latency_p90_s", "latency_mean_s"}
    for name in ("pass1_cold", "pass2_warm"):
        live = {k: v for k, v in summary["passes"][name]["overall"].items() if k not in timing}
        again = {k: v for k, v in replayed["passes"][name]["overall"].items() if k not in timing}
        assert live == again
    first = [json.loads(x)["answer"] for x in (out / "pass1_cold.jsonl").read_text().splitlines()]
    second = [
        json.loads(x)["answer"]
        for x in (tmp_path / "replay" / "pass1_cold.jsonl").read_text().splitlines()
    ]
    assert first == second


def test_cassette_roundtrip_and_unknown_request(tmp_path):
    cassette = Cassette()
    inner = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"n": 1}, headers={"X-Total-Results": "7", "Set-Cookie": "session=secret"}
        )
    )
    recording = httpx.Client(transport=RecordingTransport(cassette, inner))
    assert recording.get("https://x.org/a", params={"api_key": "SECRET", "q": "1"}).json() == {
        "n": 1
    }
    html = httpx.MockTransport(
        lambda request: httpx.Response(429, text="<html>IP 192.0.2.44</html>")
    )
    httpx.Client(transport=RecordingTransport(cassette, html)).get("https://x.org/b")

    path = tmp_path / "c.json.gz"
    cassette.save(path)
    stored = path.read_bytes()
    loaded = Cassette.load(path)
    assert len(loaded) == 2
    text = json.dumps(loaded.entries)
    assert "SECRET" not in text and "session=secret" not in text and "192.0.2.44" not in text
    assert stored[:2] == b"\x1f\x8b"  # gzip

    replay = httpx.Client(transport=ReplayTransport(loaded))
    again = replay.get("https://x.org/a", params={"api_key": "OTHER", "q": "1"})
    assert again.json() == {"n": 1} and again.headers["x-total-results"] == "7"
    assert replay.get("https://x.org/b").status_code == 429
    with pytest.raises(httpx.ConnectError, match="not in cassette"):
        replay.get("https://x.org/never-recorded")


def test_render_markdown_handles_missing_values():
    summary = {
        "benchmark": {"name": "b", "version": "1", "n_questions": 0},
        "started_utc": "s", "finished_utc": "f", "runtime_s": 0.0, "mode": "replay",
        "provider": "offline", "passes": {}, "answers_identical_across_passes": None,
    }  # fmt: skip
    assert render_markdown(summary).startswith("# Evaluation summary: b v1")
