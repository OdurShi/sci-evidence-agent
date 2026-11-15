"""Replay a recorded evaluation offline and compare it with the live run.

    python scripts/check_replay.py results/eval-2026-11-14

Runs the benchmark again with every HTTP request answered from
``<results>/http_cassette.json.gz`` (no network, no rate-limit waits) and
writes ``<results>/replay_check.json``: whether all metrics except latency and
all answers are identical to the recorded live run. This is the evidence that
the evaluation harness is deterministic given the upstream responses.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sci_evidence_agent.evaluation import run_evaluation

TIMING = {"latency_median_s", "latency_p90_s", "latency_mean_s"}


def main() -> int:
    results = Path(sys.argv[1] if len(sys.argv) > 1 else "results/eval-2026-11-14")
    live = json.loads((results / "summary.json").read_text(encoding="utf-8"))
    heldout = (live.get("heldout_planning") or {}).get("path")

    with tempfile.TemporaryDirectory() as tmp:
        replayed = run_evaluation(
            results / "benchmark_snapshot.json",
            tmp,
            provider=live["provider"],
            passes=len(live["passes"]),
            replay=results / "http_cassette.json.gz",
            heldout_path=heldout,
            echo=False,
        )
        differences: list[str] = []
        answers_total = answers_equal = 0
        for name, block in live["passes"].items():
            scopes = {**block["per_category"], "overall": block["overall"]}
            other = replayed["passes"][name]
            other_scopes = {**other["per_category"], "overall": other["overall"]}
            for scope, metrics in scopes.items():
                for key, value in metrics.items():
                    if key not in TIMING and other_scopes[scope][key] != value:
                        differences.append(f"{name}/{scope}/{key}")
            a = (results / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
            b = (Path(tmp) / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
            for x, y in zip(a, b, strict=True):
                answers_total += 1
                answers_equal += json.loads(x)["answer"] == json.loads(y)["answer"]

    check = {
        "checked_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "live_run_started_utc": live["started_utc"],
        "replay_runtime_s": replayed["runtime_s"],
        "http_requests_replayed": replayed["http_requests"],
        "metrics_compared": "every metric of every pass and category except latency",
        "metrics_identical": not differences,
        "differing_metrics": differences,
        "answers_compared": answers_total,
        "answers_identical": answers_equal,
    }
    (results / "replay_check.json").write_text(json.dumps(check, indent=2), encoding="utf-8")
    print(json.dumps(check, indent=2))
    return 0 if not differences and answers_equal == answers_total else 1


if __name__ == "__main__":
    sys.exit(main())
