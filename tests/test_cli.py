"""Command-line interface, driven in-process with mocked HTTP."""

from __future__ import annotations

import json

import httpx
import pytest

from helpers import Upstream
from sci_evidence_agent import cli
from sci_evidence_agent import http as http_layer


def test_ask_prints_answer_calls_and_identifier_check(upstream, capsys):
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    code = cli.main(["ask", "What is the molecular formula of aspirin?", "--provider", "offline"])
    out = capsys.readouterr().out
    assert code == 0
    assert "`CID 2244`" in out
    assert '-- search_compound({"query": "aspirin", "limit": 5}) -> ok, 1 record(s)' in out
    assert "-- identifiers in the answer: 2 (traceable 2, echoed 0, untraceable 0)" in out


def test_ask_json_output(upstream, capsys):
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    code = cli.main(["ask", "formula of aspirin", "--provider", "offline", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["is_model_output"] is False
    assert payload["tool_calls"][0]["tool"] == "search_compound"
    assert payload["tool_calls"][0]["raw_path"].startswith("raw/pubchem_")
    assert payload["verification"]["ok"] is True


def test_ask_reports_an_unavailable_provider(store, capsys):
    code = cli.main(["ask", "formula of aspirin", "--provider", "anthropic"])
    assert code == 1
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err


def test_default_provider_falls_back_to_offline_without_keys(upstream, capsys):
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    assert cli.main(["ask", "formula of aspirin"]) == 0
    assert "Offline planner (not a model)" in capsys.readouterr().out


def test_eval_then_verify(tmp_path, capsys, monkeypatch):
    bench = tmp_path / "bench.json"
    bench.write_text(
        json.dumps(
            {
                "name": "cli-mini",
                "version": "0",
                "questions": [
                    {
                        "id": "Q1",
                        "category": "compound",
                        "question": "formula of aspirin",
                        "expected_tools": [["search_compound"]],
                        "expected_statuses": [["ok"]],
                        "expected_identifiers": [{"kind": "cid", "value": "2244"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    fake = Upstream().add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    out = tmp_path / "run"

    # Record through a fake network, then run the CLI in replay mode.
    from sci_evidence_agent.evaluation import run_evaluation

    monkeypatch.setattr(http_layer.RateLimiter, "interval", lambda self, bucket: 0.0)
    run_evaluation(bench, out, record=True, echo=False, network=httpx.MockTransport(fake.handler))
    capsys.readouterr()

    replay_out = tmp_path / "replayed"
    code = cli.main(
        [
            "eval", "--benchmark", str(bench), "--out", str(replay_out),
            "--replay", str(out / "http_cassette.json.gz"), "--passes", "1",
        ]
    )  # fmt: skip
    text = capsys.readouterr().out
    assert code == 0
    assert "| overall | 1 | 100.0% |" in text and "mode: replay" in text

    assert cli.main(["verify", str(replay_out / "pass1_cold.jsonl")]) == 0
    assert "1 answers, 2 identifiers, 0 answer(s) need a manual check" in capsys.readouterr().out

    # Tamper with the saved answer: the verifier must object.
    path = replay_out / "pass1_cold.jsonl"
    record = json.loads(path.read_text())
    record["answer"] += " See also PMID 31234567."
    path.write_text(json.dumps(record) + "\n")
    assert cli.main(["verify", str(path)]) == 1
    assert "not in any raw response: pmid:31234567" in capsys.readouterr().out

    # A missing raw file makes the check incomplete, which is also a failure.
    for raw in (replay_out / "raw").glob("*.json"):
        raw.unlink()
    assert cli.main(["verify", str(path)]) == 1
    assert "raw response missing" in capsys.readouterr().out


def test_smoke_reports_each_check(upstream, capsys):
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    upstream.add("/compound/name/zzzznotacompound/", "pubchem/name_not_found")
    upstream.add("molecule/search.json", "chembl/molecule_search_aspirin")
    upstream.add("activity.json", "chembl/activity_CHEMBL25")
    upstream.add("uniprotkb/search", "uniprot/search_egfr_human")
    upstream.add("esearch.fcgi", "pubmed/esearch_aspirin_cyclooxygenase")
    assert cli.main(["smoke"]) == 0
    out = capsys.readouterr().out
    assert out.count("[pass]") == 6 and "6/6 checks passed" in out


def test_smoke_fails_when_a_service_is_down(upstream, capsys):
    upstream.add("pubchem", httpx.Response(503, text="busy"))
    upstream.add("ebi.ac.uk", httpx.ConnectError("down"))
    upstream.add("uniprotkb/search", "uniprot/search_egfr_human")
    upstream.add("esearch.fcgi", "pubmed/esearch_aspirin_cyclooxygenase")
    assert cli.main(["smoke"]) == 1
    out = capsys.readouterr().out
    assert out.count("[FAIL]") == 4 and "2/6 checks passed" in out


def test_ui_passes_extra_options_to_streamlit(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli.subprocess, "call", lambda command: seen.update(command=command) or 0)
    assert cli.main(["ui", "--server.port", "8765", "--server.headless", "true"]) == 0
    command = seen["command"]
    assert command[1:4] == ["-m", "streamlit", "run"]
    assert command[4].endswith("sci_evidence_agent/ui/app.py")
    assert command[5:] == ["--server.port", "8765", "--server.headless", "true"]


def test_unknown_option_is_rejected_for_other_commands(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["smoke", "--server.port", "1"])
    assert info.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_version_and_help(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out.startswith("sci-evidence 0.2.0")
    parser = cli.build_parser()
    assert {"ask", "smoke", "eval", "verify", "ui"} <= set(
        parser._subparsers._group_actions[0].choices
    )
