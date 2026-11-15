"""Provenance store: cache semantics, TTL, raw archive, call log."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx

from sci_evidence_agent import storage
from sci_evidence_agent.connectors import get_bioactivities, search_compound
from sci_evidence_agent.schemas import Status


def test_cache_key_is_stable_and_argument_sensitive():
    a = storage.cache_key("PubChem", "search_compound", {"query": "aspirin", "limit": 5})
    b = storage.cache_key("PubChem", "search_compound", {"limit": 5, "query": "aspirin"})
    c = storage.cache_key("PubChem", "search_compound", {"query": "aspirin", "limit": 6})
    d = storage.cache_key("ChEMBL", "search_compound", {"query": "aspirin", "limit": 5})
    assert a == b and len(a) == 32
    assert len({a, c, d}) == 3


def test_cache_entry_expires_after_ttl(tmp_path):
    now = [datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)]
    store = storage.Store(tmp_path, cache_ttl=timedelta(hours=24), now=lambda: now[0])
    store.cache_put("k", "PubChem", {"x": 1}, now[0].isoformat(timespec="seconds"))

    assert store.cache_get("k") == ({"x": 1}, "2026-11-14T12:00:00+00:00")
    now[0] += timedelta(hours=23, minutes=59)
    assert store.cache_get("k") is not None
    now[0] += timedelta(minutes=2)
    assert store.cache_get("k") is None  # expired, the next call must hit the network

    assert store.cache_stats()["cached_queries"] == 1
    assert store.clear_cache() == 1
    assert store.cache_stats()["cached_queries"] == 0


def test_cache_reads_can_be_switched_off_while_writes_continue(upstream, store):
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    store.read_cache = False
    first = search_compound("aspirin")
    second = search_compound("aspirin")
    assert (first.from_cache, second.from_cache) == (False, False)
    assert len(upstream.requests) == 2  # both went upstream
    store.read_cache = True
    assert search_compound("aspirin").from_cache is True  # the result had been stored
    assert len(upstream.requests) == 2


def test_unreadable_timestamp_is_a_cache_miss(tmp_path):
    store = storage.Store(tmp_path)
    store.cache_put("k", "PubChem", {"x": 1}, "not a date")
    assert store.cache_get("k") is None


def test_ok_result_is_cached_with_its_original_retrieval_time(upstream, store):
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")

    first = search_compound("aspirin")
    second = search_compound("aspirin")

    assert first.status is Status.OK and first.from_cache is False
    assert second.from_cache is True
    assert second.retrieved_at == first.retrieved_at  # not "now"
    assert second.records == first.records
    assert second.raw_path == first.raw_path
    assert len(upstream.requests) == 1  # the second call never left the process


def test_whitespace_and_case_variants_share_one_cache_entry(upstream):
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    upstream.add("activity.json", "chembl/activity_CHEMBL25")
    search_compound("aspirin")
    assert search_compound("  aspirin ").from_cache is True
    get_bioactivities("CHEMBL25", limit=3)
    assert get_bioactivities(" chembl25 ", limit=3).from_cache is True
    assert len(upstream.requests) == 2


def test_no_match_and_ambiguous_results_are_cached(upstream):
    upstream.add("/compound/name/zzzznotacompound/", "pubchem/name_not_found")
    upstream.add("/compound/name/stearate/", "pubchem/stearate_two_cids")

    assert search_compound("zzzznotacompound").status is Status.NO_MATCH
    assert search_compound("zzzznotacompound").from_cache is True
    assert search_compound("stearate").status is Status.AMBIGUOUS
    assert search_compound("stearate").from_cache is True
    assert len(upstream.requests) == 2


def test_error_result_is_never_cached(upstream):
    upstream.add(
        "/compound/name/aspirin/",
        httpx.Response(400, json={"Fault": {"Code": "PUGREST.BadRequest"}}),
        "pubchem/aspirin_by_name",
    )
    failed = search_compound("aspirin")
    retried = search_compound("aspirin")

    assert failed.status is Status.ERROR and "HTTP 400" in failed.error
    assert retried.status is Status.OK and retried.from_cache is False
    assert len(upstream.requests) == 2


def test_raw_response_is_archived_in_an_envelope(upstream, store):
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    result = search_compound("aspirin", limit=5)

    assert result.raw_path.startswith("raw/pubchem_") and result.raw_path.endswith(".json")
    key = storage.cache_key("PubChem", "search_compound", {"query": "aspirin", "limit": 5})
    assert result.raw_path == f"raw/pubchem_{key}.json"  # same key as the cache entry

    envelope = store.read_raw(result.raw_path)
    assert envelope["source"] == "PubChem"
    assert envelope["tool"] == "search_compound"
    assert envelope["arguments"] == {"query": "aspirin", "limit": 5}
    assert envelope["request_urls"][0].startswith("https://pubchem.ncbi.nlm.nih.gov/rest/pug/")
    assert envelope["response"]["PropertyTable"]["Properties"][0]["CID"] == 2244
    assert store.resolve_raw(result.raw_path).is_file()
    assert store.read_raw("raw/does_not_exist.json") is None


def test_every_call_is_logged_with_duration_and_outcome(upstream, store):
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    upstream.add("/compound/name/zzzznotacompound/", "pubchem/name_not_found")
    search_compound("aspirin", session_id="s1")
    search_compound("aspirin", session_id="s1")
    search_compound("zzzznotacompound", session_id="s2")

    calls = store.recent_calls()
    assert [c["status"] for c in calls] == ["no_match", "ok", "ok"]  # newest first
    assert [c["from_cache"] for c in calls] == [0, 1, 0]
    assert all(c["duration_ms"] >= 0 for c in calls)
    assert json.loads(calls[1]["arguments"]) == {"query": "aspirin", "limit": 10}
    assert len(store.recent_calls("s1")) == 2
    assert store.cache_stats() == {"cached_queries": 2, "logged_calls": 3}


def test_module_level_helpers_use_the_default_store(store):
    storage.init_db()
    storage.cache_put("k", "S", {"v": 1}, datetime.now(timezone.utc).isoformat())
    assert storage.cache_get("k")[0] == {"v": 1}
    storage.log_question("s", "q?", "a")
    path = storage.save_raw("S", "k", {"a": 1}, {"tool": "t"})
    assert store.read_raw(path)["response"] == {"a": 1}
    assert storage.cache_stats()["cached_queries"] == 1
    assert storage.clear_cache() == 1
    assert storage.recent_calls() == []
