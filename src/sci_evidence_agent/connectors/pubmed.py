"""PubMed connector (NCBI E-utilities).

Unlike the other sources, one search needs two requests:

    esearch   search expression -> list of PMIDs and the total hit count
    esummary  PMIDs -> title, authors, journal, date, DOI

Upstream behaviour this connector depends on (it differs from the other
databases, so copying another connector would be wrong):

* No hits is HTTP 200 with ``count = "0"`` and an empty ``idlist``, not 404.
* An invalid PMID is not an HTTP error; the record carries an ``error``
  field and the status is still 200.
* ``count`` is a string.
* The DOI sits in the ``articleids`` array under ``idtype == "doi"``.
* PubMed rewrites the expression (MeSH expansion); ``querytranslation`` is
  the query that was really executed and is returned as a warning.
* An unrecognised field tag such as ``aspirin[notafield]`` is dropped
  silently: the response has an ``errorlist`` object with empty lists and the
  search runs without the tag (observed on 2026-11-14).
* An empty expression is HTTP 200 with an ``ERROR`` string.
* Rate limit: 3 requests/second, or 10 with ``NCBI_API_KEY``.

NCBI asks clients to identify themselves (``tool`` and, optionally,
``email``); see https://www.ncbi.nlm.nih.gov/books/NBK25497/. The email and
API key are sent with the request but never stored: the HTTP layer strips
them from every URL it returns.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote_plus

from sci_evidence_agent.connectors.base import archive, clamp_limit, run_connector
from sci_evidence_agent.http import get_client
from sci_evidence_agent.schemas import (
    LiteratureRecord,
    Status,
    ToolResult,
    error_result,
    no_match_result,
)

SOURCE = "PubMed"
BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
WEB = "https://pubmed.ncbi.nlm.nih.gov"

TOOL_NAME = "sci-evidence-agent"


def _identity() -> dict[str, str]:
    """Identification parameters requested by NCBI."""
    params = {"tool": TOOL_NAME}
    email = os.environ.get("NCBI_EMAIL")
    if email:
        params["email"] = email
    key = os.environ.get("NCBI_API_KEY")
    if key:
        params["api_key"] = key
    return params


def _pick_doi(article_ids: list[dict[str, Any]]) -> str | None:
    for entry in article_ids:
        if entry.get("idtype") == "doi":
            return entry.get("value")
    return None


def _to_record(pmid: str, entry: dict[str, Any]) -> LiteratureRecord:
    doi = _pick_doi(entry.get("articleids", []))
    return LiteratureRecord(
        pmid=pmid,
        title=entry.get("title") or None,
        authors=[a.get("name") for a in entry.get("authors", []) if a.get("name")],
        journal=entry.get("fulljournalname") or entry.get("source") or None,
        pub_date=entry.get("pubdate") or None,
        doi=doi,
        doi_url=f"https://doi.org/{doi}" if doi else None,
        source_url=f"{WEB}/{pmid}/",
    )


def _fetch(args: dict[str, Any]) -> ToolResult:
    term: str = args["term"]
    sort: str = args["sort"]
    limit, limit_warnings = clamp_limit(SOURCE, args["limit"])
    warnings: list[str] = list(limit_warnings)
    client = get_client()

    # Step 1: esearch returns the PMIDs.
    search = client.get_json(
        SOURCE,
        f"{BASE}/esearch.fcgi",
        {
            **_identity(),
            "db": "pubmed",
            "term": term,
            "retmode": "json",
            "retmax": limit,
            "sort": sort,
        },
    )
    block = search.data.get("esearchresult", {})

    if block.get("ERROR"):
        result = error_result(SOURCE, args, f"PubMed rejected the query: {block['ERROR']}")
        result.source_url = search.url
        return result

    # A malformed expression is reported in ``errorlist``, not as an HTTP error.
    errors = block.get("errorlist")
    if errors is not None:
        bad_fields = errors.get("fieldsnotfound") or []
        bad_phrases = errors.get("phrasesnotfound") or []
        if bad_fields:
            result = error_result(SOURCE, args, f"invalid field in search expression: {bad_fields}")
            result.source_url = search.url
            return result
        if bad_phrases:
            warnings.append(f"terms without any PubMed hit were ignored: {bad_phrases}")
        else:
            warnings.append(
                "PubMed flagged the expression without naming a term; an unrecognised "
                "field tag is dropped silently, so check the executed query"
            )

    try:
        total = int(block.get("count", "0"))  # ``count`` is a string
    except (TypeError, ValueError):
        total = 0

    pmids: list[str] = block.get("idlist", [])
    if not pmids or total == 0:
        return no_match_result(SOURCE, args, search.url, warnings)

    # The executed query is provenance: PubMed expands terms to MeSH headings.
    translated = block.get("querytranslation")
    if translated and translated.strip() != term.strip():
        warnings.append(f"query executed by PubMed: {translated}")

    remaining = search.headers.get("x-ratelimit-remaining")
    if remaining in ("0", "1"):
        warnings.append(
            f"NCBI rate-limit headroom is {remaining}/"
            f"{search.headers.get('x-ratelimit-limit', '?')}; "
            "setting NCBI_API_KEY raises the limit from 3 to 10 requests/second"
        )

    # Step 2: esummary returns the metadata.
    summary = client.get_json(
        SOURCE,
        f"{BASE}/esummary.fcgi",
        {**_identity(), "db": "pubmed", "id": ",".join(pmids), "retmode": "json"},
    )

    raw_path = archive(
        SOURCE,
        "search_literature",
        args,
        {"esearch": search.data, "esummary": summary.data},
        [search.url, summary.url],
    )

    result_block = summary.data.get("result", {})
    records: list[dict[str, Any]] = []
    failed: list[str] = []
    for pmid in pmids:
        entry = result_block.get(pmid)
        # An invalid or withheld PMID carries an ``error`` field; HTTP is still 200.
        if not entry or entry.get("error"):
            failed.append(pmid)
            continue
        records.append(_to_record(pmid, entry).model_dump())

    if failed:
        warnings.append(f"no summary for {len(failed)} PMID(s), skipped: {failed[:5]}")

    if not records:
        return ToolResult(
            status=Status.ERROR,
            source=SOURCE,
            query=args,
            error=f"esearch reported {total} hits but esummary returned none of them",
            source_url=search.url,
            warnings=warnings,
            raw_path=raw_path,
        )

    without_doi = sum(1 for r in records if r["doi"] is None)
    if without_doi:
        warnings.append(
            f"{without_doi}/{len(records)} records have no DOI (common for older papers)"
        )

    return ToolResult(
        status=Status.OK,
        source=SOURCE,
        query=args,
        records=records,
        source_url=f"{WEB}/?term={quote_plus(term)}",
        truncated=total > len(records),
        total_available=total,
        warnings=warnings,
        raw_path=raw_path,
    )


def _fetch_by_pmid(args: dict[str, Any]) -> ToolResult:
    pmid: str = args["pmid"]

    summary = get_client().get_json(
        SOURCE,
        f"{BASE}/esummary.fcgi",
        {**_identity(), "db": "pubmed", "id": pmid, "retmode": "json"},
    )

    entry = summary.data.get("result", {}).get(pmid)
    if not entry or entry.get("error"):
        reason = (entry or {}).get("error") or summary.data.get("error")
        warnings = [f"PubMed: {reason}"] if reason else []
        return no_match_result(SOURCE, args, summary.url, warnings)

    raw_path = archive(SOURCE, "get_article", args, summary.data, [summary.url])
    record = _to_record(pmid, entry)

    return ToolResult(
        status=Status.OK,
        source=SOURCE,
        query=args,
        records=[record.model_dump()],
        source_url=record.source_url,
        total_available=1,
        warnings=[] if record.doi else ["this record has no DOI"],
        raw_path=raw_path,
    )


def search_literature(
    term: str,
    limit: int = 10,
    sort: str = "relevance",
    session_id: str | None = None,
) -> ToolResult:
    """Search PubMed.

    Args:
        term: PubMed search expression; field tags are supported, e.g.
            ``"aspirin AND cyclooxygenase"`` or ``"EGFR[Title] AND 2024[PDAT]"``.
        limit: Maximum number of records to return (at most 100).
        sort: ``"relevance"`` or ``"pub_date"``.
        session_id: Optional session identifier for the call log.
    """
    args = {"term": term.strip(), "limit": limit, "sort": sort}
    return run_connector(
        source=SOURCE,
        func_name="search_literature",
        args=args,
        fetch=lambda: _fetch(args),
        session_id=session_id,
    )


def get_article(pmid: str, session_id: str | None = None) -> ToolResult:
    """Fetch the metadata of one article by PMID.

    Args:
        pmid: PubMed ID, digits only (e.g. ``"14592543"``).
        session_id: Optional session identifier for the call log.
    """
    args = {"pmid": pmid.strip()}
    return run_connector(
        source=SOURCE,
        func_name="get_article",
        args=args,
        fetch=lambda: _fetch_by_pmid(args),
        session_id=session_id,
    )
