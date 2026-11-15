"""Deterministic evaluation harness.

Runs a benchmark of questions through the agent and scores each run with
fixed rules. No model and no randomness are involved when the offline planner
is used, so the only source of variation is the live databases; a recorded
cassette (see :mod:`sci_evidence_agent.cassette`) removes that as well.

Metrics (per category and overall):

``tool_selection_exact``
    Share of questions whose executed tool calls equal the expected calls,
    round by round (calls within a round are compared as a multiset).
``tool_precision`` / ``tool_recall``
    Micro-averaged overlap between executed and expected tool names.
``status_match``
    Share of questions whose result statuses equal the expected statuses.
``identifier_recall``
    Share of expected key identifiers that appear in the answer.
``traceable_share``
    Share of identifiers in the answers that occur in the archived raw
    responses of the same run (see :mod:`sci_evidence_agent.citations`).
``handling``
    For questions that expect a no_match, ambiguous or error state: share
    where the statuses match and the answer states that condition in the
    wording reserved for it.
``fabrication_detected``
    Negative control: six invented identifiers (one per kind) are appended to
    each answer; the share of answers for which the verifier flags all six.
``latency_median_s`` / ``latency_p90_s``
    Wall-clock time per question, including rate-limit waits.
``cache_hit_rate``
    Share of tool calls answered from the cache (meaningful for the second pass).
"""

from __future__ import annotations

import contextlib
import json
import platform
import shutil
import sqlite3
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from sci_evidence_agent import __version__, storage
from sci_evidence_agent import http as http_layer
from sci_evidence_agent.agent import AgentRun, ask
from sci_evidence_agent.cassette import Cassette, RecordingTransport, ReplayTransport
from sci_evidence_agent.citations import (
    Identifier,
    RawDocument,
    echo_text_for,
    extract_identifiers,
    verify_answer,
)
from sci_evidence_agent.planning import plan_question
from sci_evidence_agent.providers import get_provider
from sci_evidence_agent.reporting import STATE_TEXT

CATEGORY_ORDER = ("compound", "bioactivity_chain", "protein", "literature", "cross_source")
_KEY_ARGUMENTS = ("query", "chembl_id", "term", "pmid", "taxon", "inchikey", "organism_id")

# Invented identifiers for the negative control. None of them exists upstream
# in this form; what matters is that no raw response of a run contains them.
FABRICATED = (
    Identifier("cid", "987654321"),
    Identifier("chembl", "CHEMBL987654321"),
    Identifier("uniprot", "Q9ZZZ8"),
    Identifier("pmid", "98765432"),
    Identifier("doi", "10.99999/fabricated.2026.0001"),
    Identifier("inchikey", "ZZZZZZZZZZZZZZ-ZZZZZZZZZZ-Z"),
)
FABRICATED_TEXT = (
    "\n\nSee also PubChem CID 987654321, CHEMBL987654321, UniProt Q9ZZZ8, PMID 98765432, "
    "DOI 10.99999/fabricated.2026.0001 and InChIKey ZZZZZZZZZZZZZZ-ZZZZZZZZZZ-Z."
)


def load_benchmark(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "questions" not in data:
        raise ValueError(f"{path}: not a benchmark file (no 'questions' key)")
    return data


def percentile(values: list[float], q: float) -> float | None:
    """Percentile with linear interpolation between closest ranks (q in [0, 100])."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _normalise_rounds(rounds: list[list[str]]) -> list[list[str]]:
    return [sorted(r) for r in rounds]


def _aligned_statuses(run: AgentRun) -> list[list[str]]:
    """Statuses per round, ordered like the sorted tool names of that round."""
    grouped: dict[int, list[tuple[str, str]]] = {}
    for execution in run.executions:
        grouped.setdefault(execution.round_index, []).append(
            (execution.tool_name, execution.result.status.value)
        )
    return [[status for _, status in sorted(grouped[i])] for i in sorted(grouped)]


def _expected_statuses(question: dict[str, Any]) -> list[list[str]]:
    paired = [
        sorted(zip(tools, statuses, strict=True))
        for tools, statuses in zip(
            question["expected_tools"], question["expected_statuses"], strict=True
        )
    ]
    return [[status for _, status in pairs] for pairs in paired]


def score_question(
    question: dict[str, Any], run: AgentRun, wall_s: float, store: storage.Store
) -> dict[str, Any]:
    """Score one run against its benchmark entry. Pure apart from reading raw files."""
    called = run.tool_rounds()
    expected = question["expected_tools"]
    exact = _normalise_rounds(called) == _normalise_rounds(expected)

    called_flat = Counter(t for r in called for t in r)
    expected_flat = Counter(t for r in expected for t in r)
    overlap = sum((called_flat & expected_flat).values())

    statuses = _aligned_statuses(run)
    status_match = exact and statuses == _expected_statuses(question)

    # Identifier recall: expected key identifiers that appear in the answer.
    in_answer = extract_identifiers(run.answer)
    wanted = [
        Identifier(e["kind"], e["value"].lower() if e["kind"] == "doi" else e["value"])
        for e in question.get("expected_identifiers", [])
    ]
    found = [w for w in wanted if w in in_answer]

    # Traceability of everything the answer mentions.
    documents: list[RawDocument] = []
    missing_raw: list[str] = []
    for result in run.results:
        if not result.raw_path:
            continue
        envelope = store.read_raw(result.raw_path)
        if envelope is None:
            missing_raw.append(result.raw_path)
        else:
            documents.append(RawDocument(result.source, envelope.get("response"), result.raw_path))
    echo = echo_text_for(run.question, [e.arguments for e in run.executions])
    verification = verify_answer(run.answer, documents, echo)
    verification.missing_raw = missing_raw

    # Negative control: invented identifiers must all be flagged.
    probe = verify_answer(run.answer + FABRICATED_TEXT, documents, echo)
    flagged = set(probe.untraceable)
    fabrication_detected = all(f in flagged for f in FABRICATED)

    # Handling of non-ok states.
    expected_states = {s for r in question["expected_statuses"] for s in r} - {"ok"}
    handling: bool | None = None
    if expected_states:
        handling = bool(status_match) and all(
            STATE_TEXT[state] in run.answer for state in expected_states
        )

    calls = [
        {
            "round": e.round_index,
            "tool": e.tool_name,
            "arguments": e.arguments,
            "status": e.result.status.value,
            "record_count": len(e.result.records),
            "from_cache": e.result.from_cache,
            "duration_ms": e.duration_ms,
            "source": e.result.source,
            "source_url": e.result.source_url,
            "retrieved_at": e.result.retrieved_at,
            "truncated": e.result.truncated,
            "total_available": e.result.total_available,
            "warnings": e.result.warnings,
            "error": e.result.error,
            "raw_path": e.result.raw_path,
        }
        for e in run.executions
    ]

    return {
        "id": question["id"],
        "category": question["category"],
        "question": question["question"],
        "provider": run.provider_name,
        "model": run.provider_model,
        "rounds": run.rounds,
        "wall_s": round(wall_s, 3),
        "error": run.error,
        "stopped_early": run.stopped_early,
        "usage": run.usage,
        "expected_tools": expected,
        "called_tools": called,
        "tool_selection_exact": exact,
        "tool_overlap": overlap,
        "tools_called": sum(called_flat.values()),
        "tools_expected": sum(expected_flat.values()),
        "expected_statuses": question["expected_statuses"],
        "statuses": statuses,
        "status_match": status_match,
        "expected_identifiers": [str(w) for w in wanted],
        "identifiers_found": [str(w) for w in found],
        "verification": verification.to_dict(),
        "fabrication_detected": fabrication_detected,
        "handling": handling,
        "calls": calls,
        "cache_hits": sum(1 for e in run.executions if e.result.from_cache),
        "answer": run.answer,
    }


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-question records into one row of metrics."""
    n = len(records)
    if not n:
        return {"n": 0}
    walls = [r["wall_s"] for r in records]
    ids_total = sum(r["verification"]["identifiers"] for r in records)
    non_ok = [r for r in records if r["handling"] is not None]
    calls = sum(r["tools_called"] for r in records)
    median = percentile(walls, 50)
    p90 = percentile(walls, 90)
    return {
        "n": n,
        "tool_selection_exact": _ratio(sum(r["tool_selection_exact"] for r in records), n),
        "tool_precision": _ratio(sum(r["tool_overlap"] for r in records), calls),
        "tool_recall": _ratio(
            sum(r["tool_overlap"] for r in records), sum(r["tools_expected"] for r in records)
        ),
        "status_match": _ratio(sum(r["status_match"] for r in records), n),
        "identifiers_expected": sum(len(r["expected_identifiers"]) for r in records),
        "identifiers_found": sum(len(r["identifiers_found"]) for r in records),
        "identifier_recall": _ratio(
            sum(len(r["identifiers_found"]) for r in records),
            sum(len(r["expected_identifiers"]) for r in records),
        ),
        "answer_identifiers": ids_total,
        "traceable": sum(r["verification"]["traceable"] for r in records),
        "echoed": sum(r["verification"]["echoed"] for r in records),
        "untraceable": sum(r["verification"]["untraceable"] for r in records),
        "traceable_share": _ratio(sum(r["verification"]["traceable"] for r in records), ids_total),
        "possible_pmids_untraced": sum(
            len(r["verification"]["possible_pmids_untraced"]) for r in records
        ),
        "non_ok_questions": len(non_ok),
        "non_ok_handled": sum(bool(r["handling"]) for r in non_ok),
        "handling": _ratio(sum(bool(r["handling"]) for r in non_ok), len(non_ok)),
        "fabrication_detected": _ratio(sum(r["fabrication_detected"] for r in records), n),
        "latency_median_s": round(median, 3) if median is not None else None,
        "latency_p90_s": round(p90, 3) if p90 is not None else None,
        "latency_mean_s": round(statistics.fmean(walls), 3),
        "tool_calls": calls,
        "cache_hits": sum(r["cache_hits"] for r in records),
        "cache_hit_rate": _ratio(sum(r["cache_hits"] for r in records), calls),
        "run_errors": sum(1 for r in records if r["error"]),
    }


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Metrics per category (in a fixed order) and overall."""
    categories = [c for c in CATEGORY_ORDER if any(r["category"] == c for r in records)]
    categories += sorted({r["category"] for r in records} - set(categories))
    return {
        "per_category": {
            c: aggregate([r for r in records if r["category"] == c]) for c in categories
        },
        "overall": aggregate(records),
    }


def run_pass(
    questions: list[dict[str, Any]],
    provider: str,
    store: storage.Store,
    label: str,
    echo: bool = True,
) -> list[dict[str, Any]]:
    """Run every question once and score it."""
    records = []
    for question in questions:
        started = time.monotonic()
        run = ask(question["question"], provider=get_provider(provider))
        wall = time.monotonic() - started
        record = score_question(question, run, wall, store)
        records.append(record)
        if echo:
            mark = "ok " if record["tool_selection_exact"] and record["status_match"] else "MISS"
            print(
                f"  [{label}] {question['id']:<4} {mark} {wall:7.2f}s "
                f"{'+'.join('/'.join(r) for r in record['called_tools']) or '(no call)'}",
                flush=True,
            )
    return records


def _arguments_planned(plan_rounds: list[list[Any]], expected: list[dict[str, Any]]) -> bool:
    """True when every expected call is planned with exactly the listed argument values."""
    steps = [step for steps in plan_rounds for step in steps]
    return all(
        any(
            step.tool == want["tool"]
            and all(step.arguments.get(k) == v for k, v in want["arguments"].items())
            for step in steps
        )
        for want in expected
    )


def score_planning_only(questions: list[dict[str, Any]]) -> dict[str, Any]:
    """Tool selection of the offline planner without executing anything.

    Used for the held-out paraphrases. Two levels are reported: ``exact`` (the
    planned tool rounds equal the expected ones; a chained plan counts as
    planned in full, as if every lookup succeeded) and ``arguments_exact``
    (in addition, the calls listed under ``expected_arguments`` carry exactly
    those argument values). The first level alone would accept the right tool
    with the wrong query.
    """
    rows = []
    for question in questions:
        plan = plan_question(question["question"])
        planned = plan.tool_names()
        exact = _normalise_rounds(planned) == _normalise_rounds(question["expected_tools"])
        wanted = question.get("expected_arguments")
        rows.append(
            {
                "id": question["id"],
                "category": question["category"],
                "question": question["question"],
                "expected_tools": question["expected_tools"],
                "planned_tools": planned,
                "planned_arguments": [[s.arguments for s in steps] for steps in plan.rounds],
                "exact": exact,
                "arguments_exact": (
                    exact and _arguments_planned(plan.rounds, wanted)
                    if wanted is not None
                    else None
                ),
                "recognised": plan.entities.labels(),
            }
        )
    by_category: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = by_category.setdefault(row["category"], {"n": 0, "exact": 0, "arguments_exact": 0})
        entry["n"] += 1
        entry["exact"] += int(row["exact"])
        entry["arguments_exact"] += int(bool(row["arguments_exact"]))
    with_arguments = [r for r in rows if r["arguments_exact"] is not None]
    return {
        "n": len(rows),
        "exact": sum(r["exact"] for r in rows),
        "accuracy": _ratio(sum(r["exact"] for r in rows), len(rows)),
        "arguments_exact": sum(bool(r["arguments_exact"]) for r in with_arguments),
        "arguments_accuracy": _ratio(
            sum(bool(r["arguments_exact"]) for r in with_arguments), len(with_arguments)
        ),
        "per_category": by_category,
        "questions": rows,
    }


def environment() -> dict[str, Any]:
    """Software and hardware description of the evaluation run (no host or user name)."""
    import pydantic

    info: dict[str, Any] = {
        "package_version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "httpx": httpx.__version__,
        "pydantic": pydantic.__version__,
    }
    if sys.platform == "darwin":
        import subprocess

        with contextlib.suppress(OSError, subprocess.SubprocessError):
            info["cpu"] = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip()
    return info


def _export_call_log(store: storage.Store, path: Path) -> int:
    if not store.db_path.exists():
        return 0
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM call_log ORDER BY id")]
    finally:
        conn.close()
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def _fmt(value: Any, percent: bool = False) -> str:
    if value is None:
        return "n/a"
    if percent:
        return f"{100 * value:.1f}%"
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def render_markdown(summary: dict[str, Any]) -> str:
    """Human-readable tables for ``summary.md``."""
    lines = [
        f"# Evaluation summary: {summary['benchmark']['name']} v{summary['benchmark']['version']}",
        "",
        f"- Started (UTC): {summary['started_utc']}; finished (UTC): {summary['finished_utc']}",
        f"- Total runtime: {summary['runtime_s']} s; mode: {summary['mode']}; "
        f"provider: {summary['provider']}",
        f"- Questions: {summary['benchmark']['n_questions']}; passes: {len(summary['passes'])}; "
        f"HTTP requests per pass: {summary.get('http_requests', {})}",
        "- The first pass never reads the cache (every question is timed cold); later passes "
        "reuse what the first pass stored.",
        "",
    ]
    for name, block in summary["passes"].items():
        lines += [f"## Pass `{name}`", ""]
        lines.append(
            "| category | n | tool selection (exact) | tool precision | tool recall | "
            "status match | identifier recall | traceable share | handling of non-ok states | "
            "fabrication detected | latency median (s) | latency p90 (s) | cache hit rate |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        rows = [*block["per_category"].items(), ("overall", block["overall"])]
        for category, m in rows:
            recall = (
                f"{_fmt(m['identifier_recall'], True)} "
                f"({m['identifiers_found']}/{m['identifiers_expected']})"
            )
            traceable = (
                f"{_fmt(m['traceable_share'], True)} ({m['traceable']}/{m['answer_identifiers']})"
            )
            handling = (
                f"{_fmt(m['handling'], True)} ({m['non_ok_handled']}/{m['non_ok_questions']})"
            )
            hits = f"{_fmt(m['cache_hit_rate'], True)} ({m['cache_hits']}/{m['tool_calls']})"
            lines.append(
                f"| {category} | {m['n']} | {_fmt(m['tool_selection_exact'], True)} | "
                f"{_fmt(m['tool_precision'], True)} | {_fmt(m['tool_recall'], True)} | "
                f"{_fmt(m['status_match'], True)} | {recall} | {traceable} | {handling} | "
                f"{_fmt(m['fabrication_detected'], True)} | {_fmt(m['latency_median_s'])} | "
                f"{_fmt(m['latency_p90_s'])} | {hits} |"
            )
        lines.append("")
    if summary.get("answers_identical_across_passes") is not None:
        lines.append(
            f"Answers identical between the first and last pass: "
            f"{summary['answers_identical_across_passes']}/{summary['benchmark']['n_questions']}"
        )
    held = summary.get("heldout_planning")
    if held:
        lines += [
            "",
            "## Held-out paraphrases (planning only, no network)",
            "",
            f"Tool rounds exact: {held['exact']}/{held['n']} ({_fmt(held['accuracy'], True)})",
            "",
            f"Tool rounds and arguments exact: {held['arguments_exact']}/{held['n']} "
            f"({_fmt(held['arguments_accuracy'], True)})",
            "",
            "| id | category | tools | arguments | planned calls | question |",
            "|---|---|---|---|---|---|",
        ]
        for row in held["questions"]:
            planned = "; ".join(
                " + ".join(
                    f"{tool}({', '.join(str(v) for k, v in args.items() if k in _KEY_ARGUMENTS)})"
                    for tool, args in zip(tools, arguments, strict=True)
                )
                for tools, arguments in zip(
                    row["planned_tools"], row["planned_arguments"], strict=True
                )
            )
            lines.append(
                f"| {row['id']} | {row['category']} | {'yes' if row['exact'] else 'no'} | "
                f"{'yes' if row['arguments_exact'] else 'no'} | `{planned or '(no call)'}` | "
                f"{row['question']} |"
            )
    return "\n".join(lines) + "\n"


def run_evaluation(
    benchmark_path: str | Path,
    out_dir: str | Path,
    *,
    provider: str = "offline",
    passes: int = 2,
    record: bool = False,
    replay: str | Path | None = None,
    heldout_path: str | Path | None = None,
    limit: int | None = None,
    echo: bool = True,
    network: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Run the benchmark and write all outputs to ``out_dir``.

    The first pass starts from an empty cache and archive inside ``out_dir``
    and never reads the cache, so every question is answered cold; later
    passes reuse what the first pass stored. With ``replay`` no request leaves
    the machine and rate-limit waits are skipped.

    ``network`` replaces the real network transport of a live run (used by the
    tests to serve recorded responses).

    Files written: ``summary.json``, ``summary.md``, ``pass<N>_<label>.jsonl``,
    ``benchmark_snapshot.json``, ``environment.json``, ``call_log.jsonl``,
    ``raw/`` (archived raw responses) and, when recording, ``http_cassette.json.gz``.
    """
    benchmark = load_benchmark(benchmark_path)
    questions = benchmark["questions"][:limit] if limit else benchmark["questions"]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # A cold first pass needs an empty store.
    for stale in (out / "app.db", out / "raw"):
        if stale.is_dir():
            shutil.rmtree(stale)
        elif stale.exists():
            stale.unlink()

    store = storage.configure(data_dir=out)
    cassette: Cassette | None = None
    if replay:
        cassette = Cassette.load(replay)
        transport: httpx.BaseTransport | None = ReplayTransport(cassette)
        limiter = http_layer.RateLimiter(intervals={}, default_interval=0.0)
        sleep = lambda _seconds: None  # noqa: E731 - no waiting when replaying
        mode = "replay"
    else:
        mode = "live"
        limiter = http_layer.RateLimiter()
        sleep = time.sleep
        transport = network
        if record:
            cassette = Cassette()
            transport = RecordingTransport(cassette, network)
    request_count = [0]

    def count_request(_request: httpx.Request) -> None:
        request_count[0] += 1

    client = httpx.Client(
        transport=transport, follow_redirects=True, event_hooks={"request": [count_request]}
    )
    http_layer.set_client(http_layer.HttpClient(client=client, limiter=limiter, sleep=sleep))

    started = datetime.now(timezone.utc)
    clock = time.monotonic()
    labels = ["cold", *[f"warm{i if passes > 2 else ''}" for i in range(1, passes)]]
    all_passes: dict[str, list[dict[str, Any]]] = {}
    http_requests: dict[str, int] = {}
    try:
        for index in range(passes):
            label = labels[index]
            if echo:
                print(
                    f"pass {index + 1}/{passes} ({label}), {len(questions)} questions", flush=True
                )
            pass_started = time.monotonic()
            # Cold pass: no cache lookups at all, so a question never benefits from an
            # earlier one. Results are still written and serve the following passes.
            store.read_cache = index > 0
            before = request_count[0]
            records = run_pass(questions, provider, store, label, echo)
            name = f"pass{index + 1}_{label}"
            all_passes[name] = records
            http_requests[name] = request_count[0] - before
            with (out / f"{name}.jsonl").open("w", encoding="utf-8") as handle:
                for rec in records:
                    handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if echo:
                print(f"  pass finished in {time.monotonic() - pass_started:.1f}s", flush=True)
    finally:
        http_layer.set_client(None)
        client.close()

    runtime = time.monotonic() - clock
    names = list(all_passes)
    identical = None
    if len(names) > 1:
        first, last = all_passes[names[0]], all_passes[names[-1]]
        identical = sum(a["answer"] == b["answer"] for a, b in zip(first, last, strict=True))

    summary: dict[str, Any] = {
        "benchmark": {
            "name": benchmark.get("name"),
            "version": benchmark.get("version"),
            "verified_on": benchmark.get("verified_on"),
            "path": str(benchmark_path),
            "n_questions": len(questions),
        },
        "provider": provider,
        "mode": mode,
        "started_utc": started.isoformat(timespec="seconds"),
        "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runtime_s": round(runtime, 1),
        "pass_runtime_s": {
            name: round(sum(r["wall_s"] for r in records), 1)
            for name, records in all_passes.items()
        },
        "http_requests": http_requests,
        "environment": environment(),
        "passes": {name: summarise(records) for name, records in all_passes.items()},
        "answers_identical_across_passes": identical,
    }
    if heldout_path:
        heldout = load_benchmark(heldout_path)
        summary["heldout_planning"] = score_planning_only(heldout["questions"])
        summary["heldout_planning"]["path"] = str(heldout_path)

    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "summary.md").write_text(render_markdown(summary), encoding="utf-8")
    (out / "benchmark_snapshot.json").write_text(
        json.dumps(benchmark, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "environment.json").write_text(
        json.dumps(summary["environment"], indent=2), encoding="utf-8"
    )
    _export_call_log(store, out / "call_log.jsonl")
    if cassette is not None and record and not replay:
        cassette.save(out / "http_cassette.json.gz")

    # The SQLite file is a cache; its content is exported above, so drop it.
    (out / "app.db").unlink(missing_ok=True)
    storage.set_store(None)
    return summary
