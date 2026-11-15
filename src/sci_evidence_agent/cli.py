"""Command-line interface.

sci-evidence ask "What is the molecular formula of aspirin?"
sci-evidence smoke [--raw]
sci-evidence eval --benchmark benchmark/questions.json --out results/my-run
sci-evidence verify results/my-run/pass1_cold.jsonl
sci-evidence ui
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from sci_evidence_agent import __version__, config


def _cmd_ask(args: argparse.Namespace) -> int:
    from sci_evidence_agent.agent import ask
    from sci_evidence_agent.citations import verify_run

    run = ask(args.question, provider=args.provider)
    if args.json:
        payload: dict[str, Any] = {
            "question": run.question,
            "provider": run.provider_name,
            "model": run.provider_model,
            "is_model_output": run.is_real_model,
            "rounds": run.rounds,
            "answer": run.answer,
            "error": run.error,
            "tool_calls": [
                {
                    "tool": e.tool_name,
                    "arguments": e.arguments,
                    "status": e.result.status.value,
                    "duration_ms": e.duration_ms,
                    "from_cache": e.result.from_cache,
                    "raw_path": e.result.raw_path,
                }
                for e in run.executions
            ],
            "verification": verify_run(run).to_dict(),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1 if run.error else 0

    if run.error:
        print(f"error: {run.error}", file=sys.stderr)
        return 1
    print(run.answer)
    print()
    print(f"-- {run.provider_name} · {run.provider_model} · {run.rounds} round(s)")
    for e in run.executions:
        cache = " (cache)" if e.result.from_cache else ""
        print(
            f"-- {e.tool_name}({json.dumps(e.arguments, ensure_ascii=False)}) -> "
            f"{e.result.status.value}, {len(e.result.records)} record(s), {e.duration_ms} ms{cache}"
        )
    report = verify_run(run)
    counts = report.counts()
    print(
        f"-- identifiers in the answer: {counts['identifiers']} "
        f"(traceable {counts['traceable']}, echoed {counts['echoed']}, "
        f"untraceable {counts['untraceable']})"
    )
    for identifier in report.untraceable:
        print(f"-- NOT TRACEABLE to any raw response: {identifier}")
    return 0 if report.ok else 2


def _cmd_smoke(args: argparse.Namespace) -> int:
    from sci_evidence_agent.smoke import run_smoke

    results = run_smoke(raw=args.raw)
    return 0 if all(ok for _, ok, _ in results) else 1


def _cmd_eval(args: argparse.Namespace) -> int:
    from sci_evidence_agent.evaluation import render_markdown, run_evaluation

    summary = run_evaluation(
        args.benchmark,
        args.out,
        provider=args.provider,
        passes=args.passes,
        record=args.record,
        replay=args.replay,
        heldout_path=args.heldout,
        limit=args.limit,
    )
    print()
    print(render_markdown(summary))
    print(f"Outputs written to {args.out}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from sci_evidence_agent.citations import RawDocument, echo_text_for, verify_answer

    path = Path(args.results)
    root = Path(args.raw_root) if args.raw_root else path.parent
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    failures = 0
    total = 0
    for line in lines:
        record = json.loads(line)
        documents, missing = [], []
        for call in record.get("calls", []):
            raw_path = call.get("raw_path")
            if not raw_path:
                continue
            try:
                envelope = json.loads((root / raw_path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                missing.append(raw_path)
                continue
            documents.append(RawDocument(call.get("source", ""), envelope.get("response")))
        echo = echo_text_for(record["question"], [c["arguments"] for c in record.get("calls", [])])
        report = verify_answer(record.get("answer", ""), documents, echo, strict=args.strict)
        counts = report.counts()
        total += counts["identifiers"]
        mark = "ok  " if report.ok and not missing else "FAIL"
        print(
            f"{mark} {record.get('id', '?'):<5} identifiers {counts['identifiers']:>3}  "
            f"traceable {counts['traceable']:>3}  echoed {counts['echoed']:>2}  "
            f"untraceable {counts['untraceable']:>2}"
        )
        for identifier in report.untraceable:
            print(f"       not in any raw response: {identifier}")
        for number in report.possible_pmids_untraced:
            print(f"       unlabelled number, possibly a PMID, not in any raw response: {number}")
        for raw_path in missing:
            print(f"       raw response missing, check incomplete: {raw_path}")
        if not report.ok or missing:
            failures += 1
    print(f"{len(lines)} answers, {total} identifiers, {failures} answer(s) need a manual check")
    return 1 if failures else 0


def _cmd_ui(args: argparse.Namespace) -> int:
    app = Path(__file__).parent / "ui" / "app.py"
    # Everything after "ui" is passed to Streamlit, e.g. --server.port 8502.
    command = [sys.executable, "-m", "streamlit", "run", str(app), *args.streamlit_args]
    return subprocess.call(command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sci-evidence",
        description="Database-grounded answers with archived provenance.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ask", help="answer one question")
    p.add_argument("question")
    p.add_argument(
        "--provider",
        default=None,
        help="anthropic, openai or offline (default: LLM_PROVIDER, falling back to offline)",
    )
    p.add_argument("--json", action="store_true", help="print a JSON document")
    p.set_defaults(func=_cmd_ask)

    p = sub.add_parser("smoke", help="check that the upstream services are reachable (live)")
    p.add_argument("--raw", action="store_true", help="bypass the package HTTP layer")
    p.set_defaults(func=_cmd_smoke)

    p = sub.add_parser("eval", help="run a benchmark and write results")
    p.add_argument("--benchmark", default="benchmark/questions.json")
    p.add_argument("--out", required=True, help="output directory (cache and archive start empty)")
    p.add_argument("--provider", default="offline")
    p.add_argument("--passes", type=int, default=2, help="1 = cold only, 2 = cold then warm")
    p.add_argument("--record", action="store_true", help="save the HTTP exchanges as a cassette")
    p.add_argument("--replay", default=None, help="answer all requests from this cassette")
    p.add_argument("--heldout", default=None, help="paraphrase file scored on planning only")
    p.add_argument("--limit", type=int, default=None, help="only the first N questions")
    p.set_defaults(func=_cmd_eval)

    p = sub.add_parser("verify", help="check identifiers in saved answers against raw responses")
    p.add_argument("results", help="a pass*.jsonl file written by 'eval'")
    p.add_argument("--raw-root", default=None, help="directory that contains raw/ (default: same)")
    p.add_argument(
        "--strict", action="store_true", help="also fail on unlabelled 7-8 digit numbers"
    )
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser(
        "ui", help="start the Streamlit interface (extra options are passed to Streamlit)"
    )
    p.set_defaults(func=_cmd_ui)

    return parser


def main(argv: list[str] | None = None) -> int:
    config.load_env()
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if args.command == "ui":
        args.streamlit_args = extra
    elif extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
