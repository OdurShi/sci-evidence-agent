# Notes

What the evaluation covered and did not cover, how this repository differs
from the prototype it was built from, and what is known to be weak.

## 1. The evaluation run

| | |
|---|---|
| Date | 2026-11-14, 22:47:04 to 22:49:39 UTC (154.8 s) |
| Machine | Apple M5, 32 GB RAM, macOS 27.2.1 (arm64) |
| Software | Python 3.12.16, httpx 0.28.1, pydantic 2.14.3, package version 0.2.0 |
| Planner | offline keyword planner; no model, no API key of any kind |
| Settings | `SCI_EVIDENCE_WDQS_INTERVAL=65`; no `NCBI_API_KEY`; everything else default |
| Upstream | live public APIs; UniProt release 2026_04 |
| Requests | 53 in the first pass, 2 in the second |
| Outputs | `results/eval-2026-11-14/` |

Things to keep in mind when reading the numbers:

- **One run.** Every question was asked once per pass. Latencies are single
  samples from one machine and one network connection; with 6 or 7 questions
  per category, the 90th percentile is an interpolation between the two
  slowest questions.
- **The run was repeated once.** A first run on the same day let questions in
  the first pass reuse cache entries written by earlier questions (4 of 45
  calls), so its "cold" latencies were not cold. The harness was changed to
  ignore the cache in the first pass, the keyless PubMed interval was raised
  from 0.40 s to 0.60 s (the `X-RateLimit-Remaining` header had read 0 on 11
  of 15 PubMed requests; in the repeated run it never fell below 1), the
  wording of failed requests in the report was shortened, and the run was
  repeated. The reported files are from the second
  run. Tool selection, statuses, identifier recall and the two echoed
  identifiers were the same in both runs.
- **In-sample.** The 34 benchmark questions and the planner's rules were
  written together, and the planner selected the expected tools for all 34 on
  its first try. That score says the pipeline works for these phrasings; it
  says nothing about language understanding. The 20 held-out paraphrases
  (`benchmark/heldout_paraphrases.json`) were written by the same author,
  after the rules but before the planner was scored on them, and the rules
  were not changed afterwards: 18/20 tool rounds, but only 10/20 with the
  right arguments. The argument check was
  added after the first scoring, when it became clear that "right tool, wrong
  query" was being counted as correct.
- **Traceability is by construction for the offline planner.** Its report
  only prints what tools returned, so 100% minus echoed identifiers is the
  expected value; the measurement checks the archive and the verifier, not
  the planner. The negative control (invented identifiers appended to each
  answer) is what shows that the verifier can fail.
- **Expected identifiers can drift.** `L03` expects the top relevance hit of a
  PubMed search, and `P06` and `C06` expect current candidate lists. They held
  on the verification date.
- **Three LOTUS questions.** The first pass sent three SPARQL queries, 65 s
  apart; that wait is included in the latency of `X05` and `X06`.

Other live checks on the same day and machine, outside the benchmark:

- `pytest -m "live and not lotus"`: 23 passed in 22.9 s (21 connector states
  on PubChem, ChEMBL, UniProt and PubMed, one cache check, one offline chain
  with identifier verification).
- `pytest -m lotus`: 1 passed in 128 s (three SPARQL queries with the default
  61 s spacing, none rejected).
- `sci-evidence smoke`: 6 of 6 checks passed.

## 2. Not exercised

- **Anthropic provider.** Never called: no API key was read or used, and the
  `anthropic` package was not installed in the environment used here. The
  code was carried over from the prototype (whose own README reports a run
  with `claude-opus-5-5` on 2026-10-30); only `available()` without a key is
  tested.
- **OpenAI-compatible provider.** Never called, here or in the prototype. The
  `openai` package was not installed. The version bounds of both optional
  dependencies in `pyproject.toml` are therefore unverified.
- **The agent loop with a real model.** Covered only with a scripted fake
  provider in `tests/test_agent.py` (tool calls, refusal, round limit, usage
  accounting, provider exception).
- **Streamlit interface.** Start-up and one offline question run through
  Streamlit's `AppTest` harness (`tests/test_ui.py`), and `sci-evidence ui`
  was started once headless (health endpoint and index page answered HTTP
  200, streamlit 1.67.0). Nobody looked at it in a browser: layout, links,
  example buttons, provider switch and the clear-cache button are untested.
- **CI.** `.github/workflows/ci.yml` has not run on GitHub. The same commands
  (`ruff check .`, `ruff format --check .`, `pytest`) pass locally on Python
  3.12.16 and 3.10.22, both macOS arm64. Linux and Windows were not tried.
- **Real upstream failures.** HTTP 5xx, HTTP 429 with a short `Retry-After`
  and timeouts are tested with hand-written responses
  (`tests/fixtures/synthetic/`). Seen for real: UniProt HTTP 400 (in the
  benchmark) and one Wikidata HTTP 429 with `Retry-After: 1000` during
  development.
- **`NCBI_API_KEY` / `NCBI_EMAIL`.** Mocked tests only (including the check
  that neither value is stored).
- **Cache expiry** was tested with a fake clock, not by waiting 24 hours.
- **Large result sets.** No call asked for more than 10 records; the limit
  clamps (ChEMBL 1000, UniProt 500, PubMed 100, LOTUS 200) were not hit live.
- **Concurrency.** Tool calls of one round run one after another. The rate
  limiter is thread-safe by design but was not stress-tested, and it is per
  process: two processes started within a minute can still exceed the
  Wikidata limit.
- **Chart palette check.** The two series colours are the first two slots of
  a documented colour-blind-safe palette; its validator script needs Node.js,
  which was not available, so it was not run.

## 3. Changes in behaviour compared with the prototype

The tool names, their signatures and defaults, the `ToolResult` fields, the
four states, what is cached (ok / no_match / ambiguous, 24 h) and what is not
(error), the cache key, the five-round limit, the whitelist, and provider
selection through `LLM_PROVIDER` are unchanged. Everything else that behaves
differently is listed here. Items marked **(defect)** fix something that was
wrong.

Packaging and interface

1. The flat scripts are an installable package. `tools/` became
   `connectors/`; tool specs, registry and defaults moved from `agent.py` to
   `tools.py`; `_execute` and `_compact` became `execution.ToolExecutor` and
   `compact_view`; the offline provider was split into `planning.py` (rules)
   and `reporting.py` (templates).
2. The scripts became commands: `smoke_test.py` is `sci-evidence smoke
   [--raw]` (its Claude API check was dropped because it needs a key);
   `test_connectors.py` and `test_lotus.py` are `pytest -m "live and not
   lotus"` and `pytest -m lotus`; `test_agent.py` is covered by the offline
   tests and `sci-evidence eval`; `capture_results.py` is `sci-evidence eval
   --provider ...` (token usage per question is recorded, the hard-coded
   price table and cost estimate were dropped, and there is no side-by-side
   provider report); `verify_citations.py` is `sci-evidence verify`.
3. All text is English: system prompt, tool descriptions, warnings, errors,
   summaries, UI. The system prompt now also names LOTUS and asks for answers
   in the language of the question. The planner still reads the Chinese
   keywords.
4. Data live in `./data` of the working directory or in
   `SCI_EVIDENCE_DATA_DIR`, no longer next to the code. `raw_path` is relative
   to that directory (`raw/pubchem_<key>.json`, formerly `data/raw/...`).
5. A raw archive file is an envelope (source, tool, arguments, request URLs,
   time, response) instead of the bare response.
6. `.env` is read only from the working directory, and only by the command
   line and the UI. Empty values count as unset.

HTTP layer

7. Connection errors are retried like timeouts (formerly failed at once).
8. `Retry-After` is honoured up to 60 s (formerly cut to 10 s, i.e. retried
   earlier than asked); above 60 s the call fails at once for every source
   (formerly only for SPARQL). **(defect)** An HTTP-date `Retry-After` no
   longer raises.
9. **(defect)** SPARQL requests were not rate-limited at all; the 5 s
   interval only applied to the entity lookups. Now: Wikidata Query Service
   61 s, Wikidata Action API 1 s.
10. Keyless PubMed interval 0.60 s (was 0.40 s). **(defect)** The interval is
    read per request; formerly at import time, before `.env` was loaded, so a
    key in `.env` never shortened it.
11. **(defect)** One lock serialised all sources, so waiting for one service
    delayed the others. Buckets are now independent.
12. **(defect)** `api_key` and `email` were part of the URL stored in
    `source_url` of PubMed no_match and error results, and from there in the
    cache database. They are removed from every stored URL.
13. Error messages start with the source name; HTML error pages are reduced to
    text and IP addresses in them are masked.
14. User-Agent `sci-evidence-agent/0.2.0 (research software)`; NCBI `tool`
    parameter `sci-evidence-agent`. One shared HTTP client (connection reuse).

Connectors

15. PubChem: **(defect)** a CID without a live compound record (HTTP 200 with
    only the CID) returned `ok` with an empty record; now `no_match`.
    **(defect)** Names are percent-encoded in the URL path.
16. ChEMBL: an activity without assay ID has no source URL (formerly a URL
    ending in `/None/`).
17. UniProt: **(defect)** the query is parenthesised before filters are added;
    `gene:EGFR OR gene:ERBB2` with an organism filter formerly returned 1152
    cross-species hits instead of 2. **(defect)** An inactive accession (HTTP
    200, `entryType: Inactive`) returned `ok` with an empty record; now
    `no_match` with the reason. A secondary accession adds a warning.
18. PubMed: **(defect)** an empty expression (HTTP 200 with `ERROR`) returned
    `no_match`; now `error`. An `errorlist` with empty lists (PubMed's answer
    to an unknown field tag, which it drops silently) adds a warning.
    `no_match` results keep their warnings. `source_url` is URL-encoded.
19. LOTUS: **(defect)** the raw archive held two counts instead of the
    responses, so nothing from LOTUS could be traced; it now holds the SPARQL
    result and the used parts of the Wikidata items. **(defect)** The taxon
    name was pasted into the SPARQL query unescaped. **(defect)**
    `search_occurrences` could never report truncation (it compared the row
    count with the limit it had asked for); it now asks for one row more. A
    malformed InChIKey is an `error` without a request (formerly a query that
    ended `no_match`).
20. **(defect)** The archive file was keyed by the raw arguments and the cache
    by the trimmed ones, so `" aspirin"` cached under one key and archived
    under another.

Agent, planner, report

21. Arguments are validated against the tool schema before the call (types,
    enum, unknown and missing keys). **(defect)** A non-string argument from a
    provider without strict mode formerly crashed the run.
22. An exception inside a tool is an `error` result of that call (formerly
    the whole run ended with `run.error`).
23. **(defect)** `ToolCallLog.duration_ms` was always 0; it is measured now.
24. The question is logged also when a run ends early (refusal, provider
    unavailable).
25. Planner keywords: **(defect)** ASCII keywords matched as substrings, so
    "ki" fired inside "kinase" and "gene" inside "general"; they match whole
    words now. The price is that forms outside the list ("bioassay",
    "genetic") no longer trigger an intent. Added keywords: 化学, chemical, formula, cid, inchi (compound);
    doi, journal, publication(s), citation(s), reference(s), pmid(s)
    (literature); kd, potency, bioactivities, assays (activity); proteins,
    genes, swissprot, trembl (protein); a new natural-product intent.
26. Planner entities: typed identifiers (CID, ChEMBL ID, accession, PMID, DOI,
    InChIKey), quoted phrases, multi-word names and English stop words. The
    prototype took the first ASCII word of three or more letters, which in an
    English question is "What" or "Find".
27. Planner slots: a compound name and a gene symbol in one question go to
    their own databases, and the literature query joins them with AND. The
    prototype sent one entity to every tool.
28. Planner, other: organisms are recognised (human stays the default and the
    report says so; "all species" removes the filter); PMIDs go to
    `get_article`; a DOI becomes a `[doi]` search; LOTUS tools are planned
    (the prototype's planner never called them); a gene-like token alone
    triggers a protein search only when no intent keyword is present; a
    lower-case protein name is searched as free text instead of `gene:`.
29. **(defect)** A chain continued with the first ChEMBL candidate even when
    the search was `ambiguous`. It now stops and lists the candidates.
30. Report: up to 10 records per result (was 3) with more fields (InChIKey,
    SMILES, DOI, target and assay IDs, references); candidates of an
    `ambiguous` result are listed (formerly only counted); fixed, distinct
    sentences for `no_match`, `ambiguous` and `error`; skipped steps are
    explained.

Verifier

31. **(defect)** PMIDs were "any 7-8 digit number", which missed shorter PMIDs
    and matched activity IDs; CIDs and InChIKeys were not checked; the
    accession pattern covered only one of the two formats; a DOI containing a
    parenthesis was cut at it. See `citations.py` for the current rules.
32. Numeric identifiers are only accepted from a raw response of the right
    source. Identifiers supplied by the user are a separate class (`echoed`).
    Answers of the offline planner are checked too (formerly skipped). A
    missing raw file is reported.

UI

33. English labels; the identifier check is shown under the answer.

Housekeeping without visible effect: SQLite connections are closed after use
(formerly left to the garbage collector).

## 4. Known limits

- **The offline planner does not understand language.** Any word outside its
  stop-word list becomes an entity: "string", "weighs", "potent", "Compare"
  were all sent to a database in the held-out set. It cannot translate names
  (阿司匹林), and "PubMed ID 123" is not read as a PMID. It is a regression
  fixture.
- **The verifier checks presence, not meaning.** A traceable identifier
  occurred in a raw response of the run; whether the sentence around it is
  right is not checked. Gene symbols shaped like accessions (`B3GAT1`) are
  treated as accessions. DOIs containing `<` or `>` are compared on their
  prefix only. Unlabelled 7-8 digit numbers are reported separately and only
  fail the check in strict mode.
- **Raw files are overwritten** when a query is repeated after the cache
  expired; an old answer then points at newer evidence.
- **LOTUS.** One SPARQL query per 61 s makes it unsuitable for interactive
  use; `search_by_taxon` reads an unordered sample of `limit * 4` rows, so
  which compounds are returned can change; a taxon name shared by two
  Wikidata items is not detected.
- **PubChem** names containing `/` are sent percent-encoded, which PubChem
  does not always resolve.
- **No pagination** and no abstracts (PubMed metadata only).

## 5. Open decisions

- **Raw responses.** `results/*/raw/` (2.0 MB) and `results/*/http_cassette.json.gz`
  (0.5 MB) are bulk copies of third-party data and are ignored by git. They
  are needed for `sci-evidence verify` and for the offline replay. Publishing
  them means publishing ChEMBL data (CC BY-SA 3.0), UniProt entries (CC BY
  4.0), PubMed metadata and Wikidata items alongside MIT-licensed code; the
  smaller excerpts in `tests/fixtures/` and in the `pass*.jsonl` files are
  already part of the repository.
- **Contact address.** Wikimedia and NCBI ask automated clients for a
  descriptive User-Agent with contact information. The default has none; set
  `SCI_EVIDENCE_USER_AGENT` (and `NCBI_EMAIL`) for anything beyond occasional
  use.
- **Names.** Package and repository `sci-evidence-agent`, command
  `sci-evidence`, benchmark `sci-evidence-bench`, version 0.2.0.
- **Human default.** A protein question without an organism is searched in
  human (taxonomy ID 9606), as in the prototype; the report says so.
