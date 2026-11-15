# Results

`eval-2026-11-14/` is the evaluation run reported in the top-level README:
the offline planner on `benchmark/questions.json` against the live APIs,
started 2026-11-14 22:47 UTC. Scope, machine and caveats: `docs/notes.md`.

| File | Content |
|---|---|
| `summary.json` | all metrics per pass, per category and overall; run metadata; the held-out planning scores |
| `summary.md` | the same as tables |
| `pass1_cold.jsonl`, `pass2_warm.jsonl` | one record per question: tool calls with arguments, states, durations and raw paths; the answer; the identifier check |
| `benchmark_snapshot.json` | the benchmark file as it was run |
| `environment.json` | package and Python versions, platform, CPU |
| `call_log.jsonl` | the tool-call log of both passes, exported from SQLite |
| `replay_check.json` | outcome of replaying the recorded HTTP exchanges offline (`scripts/check_replay.py`) |
| `prototype_planner_baseline.json` | the prototype's keyword planner on the same questions, planning only |
| `raw/` | archived raw API responses, one file per successful query (not committed by default) |
| `http_cassette.json.gz` | every HTTP exchange of the run, for offline replay (not committed by default) |

`raw/` and `http_cassette.json.gz` are listed in `.gitignore`: they are bulk
copies of third-party data and publishing them is a licensing decision. The
`verify` and replay commands need them; without them the summary files can
still be read, and the run can be repeated live.

The records in these files come from PubChem, ChEMBL (CC BY-SA 3.0), UniProt
(CC BY 4.0), PubMed and LOTUS/Wikidata (CC0) and stay under those terms; see
the Acknowledgements section of the top-level README.
