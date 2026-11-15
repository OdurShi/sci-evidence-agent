"""LOTUS connector (natural products, via Wikidata).

LOTUS is an open (CC0) resource of referenced structure-organism pairs:
"compound X was reported in taxon Y", each backed by a literature reference.
The curated data live in Wikidata, which is why this connector talks to the
Wikidata Query Service (SPARQL) and the Wikidata Action API.

Upstream behaviour this connector depends on:

1. Reference items are not usable through SPARQL: ``?ref wdt:P356 ?doi``
   returns almost nothing, although the same item has a DOI in the Action
   API. A lookup therefore takes two steps: SPARQL for the pairs and the
   reference QIDs, then ``wbgetentities`` (50 items per request) for labels
   and reference details. Skipping the second step yields a dataset "without
   references", which defeats the purpose of LOTUS.
2. The query service enforces a strict usage limit. On 2026-11-14 a second
   query 8 s after the first was answered with HTTP 429 and
   ``Retry-After: 1000``. A long block is reported immediately instead of
   being waited out, and requests are spaced by the HTTP layer
   (``SCI_EVIDENCE_WDQS_INTERVAL``, default 61 s).
3. Heavy queries time out (HTTP 504). The queries below avoid
   ``SERVICE wikibase:label`` and keep OPTIONAL clauses to a minimum.
4. ``?stmt ps:P703 ?taxon`` is required. Without it compound and taxon are
   not bound to the same statement and unrelated organisms are returned
   without any error.

Wikidata properties used:
    P703  found in taxon        P225  taxon name
    P235  InChIKey              P233  canonical SMILES
    P248  stated in (reference) P356  DOI
    P698  PubMed ID             P1476 title            P577  publication date
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from sci_evidence_agent.connectors.base import archive, clamp_limit, run_connector
from sci_evidence_agent.http import TIMEOUT, RetryPolicy, UpstreamError, get_client
from sci_evidence_agent.schemas import (
    OccurrenceRecord,
    Status,
    ToolResult,
    error_result,
    no_match_result,
)

SOURCE = "LOTUS"
SPARQL_EP = "https://query.wikidata.org/sparql"
API_EP = "https://www.wikidata.org/w/api.php"
ENTITY_URL = "https://www.wikidata.org/wiki/"

SPARQL_BUCKET = "Wikidata-SPARQL"
API_BUCKET = "Wikidata-API"

# Maximum number of items per wbgetentities request.
BATCH = 50

# Properties read from Wikidata items; only these are kept in the raw archive.
USED_PROPERTIES = ("P235", "P233", "P356", "P698", "P577", "P1476")

INCHIKEY_RE = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")

# SPARQL: retry only a short rate-limit pause. A timeout or HTTP 504 means the
# query is too heavy, and repeating it would only add load.
SPARQL_POLICY = RetryPolicy(
    max_attempts=3,
    backoff_base=5.0,
    backoff_cap=15.0,
    retry_after_limit=60.0,
    retry_statuses=frozenset({429}),
    retry_timeouts=False,
    retry_transport_errors=False,
)


def _sparql_literal(text: str) -> str:
    """Quote a Python string as a SPARQL string literal."""
    if any(ch in text for ch in "\n\r\t"):
        raise UpstreamError("LOTUS: control characters are not allowed in a taxon name")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _sparql(query: str) -> tuple[dict[str, Any], str]:
    """Run one SPARQL query. Returns the parsed response and the request URL."""
    try:
        resp = get_client().get_json(
            SOURCE,
            SPARQL_EP,
            {"query": query, "format": "json"},
            accept="application/sparql-results+json",
            timeout=TIMEOUT * 3,  # SPARQL is much slower than plain REST
            policy=SPARQL_POLICY,
            bucket=SPARQL_BUCKET,
        )
    except UpstreamError as exc:
        text = str(exc)
        if "HTTP 504" in text or "timed out" in text:
            raise UpstreamError(f"{text} Lower the limit or simplify the query.") from exc
        if "HTTP 429" in text:
            raise UpstreamError(
                f"{text} LOTUS via Wikidata is not suited to frequent interactive queries; "
                "collect in slow offline batches."
            ) from exc
        raise
    try:
        resp.data["results"]["bindings"]
    except (KeyError, TypeError) as exc:
        raise UpstreamError("LOTUS: unexpected SPARQL response shape") from exc
    return resp.data, resp.url


def _qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1] if uri else ""


def _fetch_entities(qids: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch Wikidata items in batches of 50.

    Reference details are only available here; see point 1 in the module docstring.
    """
    out: dict[str, dict[str, Any]] = {}
    unique = [q for q in dict.fromkeys(qids) if q]
    client = get_client()

    for i in range(0, len(unique), BATCH):
        chunk = unique[i : i + BATCH]
        resp = client.get_json(
            SOURCE,
            API_EP,
            {
                "action": "wbgetentities",
                "ids": "|".join(chunk),
                "format": "json",
                "props": "labels|claims",
                "languages": "en",
            },
            bucket=API_BUCKET,
        )
        out.update(resp.data.get("entities", {}))
    return out


def _trim_entity(entity: dict[str, Any]) -> dict[str, Any]:
    """Reduce a Wikidata item to the parts this connector reads.

    A full item is 50-250 kB; only the English label and the main value of the
    properties in ``USED_PROPERTIES`` are consumed, so only those are archived.
    """
    claims = entity.get("claims", {})
    kept: dict[str, list[dict[str, Any]]] = {}
    for pid in USED_PROPERTIES:
        statements = claims.get(pid)
        if statements:
            kept[pid] = [
                {"mainsnak": s.get("mainsnak", {}), "rank": s.get("rank")} for s in statements
            ]
    return {
        "id": entity.get("id"),
        "labels": {"en": entity["labels"]["en"]} if "en" in entity.get("labels", {}) else {},
        "claims": kept,
    }


def _claim(entity: dict[str, Any], pid: str) -> Any:
    claims = entity.get("claims", {})
    if pid not in claims:
        return None
    value = claims[pid][0].get("mainsnak", {}).get("datavalue", {}).get("value")
    if isinstance(value, dict):
        return value.get("text") or value.get("time") or value.get("id")
    return value


def _label(entity: dict[str, Any]) -> str | None:
    return entity.get("labels", {}).get("en", {}).get("value")


def _build_citation(qid: str, entity: dict[str, Any]) -> dict[str, Any]:
    doi = _claim(entity, "P356")
    pmid = _claim(entity, "P698")
    date = _claim(entity, "P577") or ""
    # Wikidata times look like +2017-06-01T00:00:00Z.
    year = date[1:5] if date.startswith("+") and len(date) >= 5 else None
    return {
        "wikidata_id": qid,
        "title": _claim(entity, "P1476") or _label(entity),
        "doi": doi,
        "pmid": str(pmid) if pmid else None,
        "year": year,
        "url": f"https://doi.org/{doi}" if doi else f"{ENTITY_URL}{qid}",
    }


def _fetch_by_taxon(args: dict[str, Any]) -> ToolResult:
    taxon: str = args["taxon"]
    limit, limit_warnings = clamp_limit(SOURCE, args["limit"])
    warnings: list[str] = list(limit_warnings)

    # Keep the query light: no label service, few OPTIONALs (otherwise HTTP 504).
    # The line ``?stmt ps:P703 ?taxonItem`` must stay; without it the result is
    # a cross product of unrelated compounds and taxa.
    data, sparql_url = _sparql(
        f"""
SELECT ?compound ?inchikey ?smiles ?ref ?taxonItem WHERE {{
  ?taxonItem wdt:P225 {_sparql_literal(taxon)} .
  ?compound p:P703 ?stmt .
  ?stmt ps:P703 ?taxonItem .
  OPTIONAL {{ ?compound wdt:P235 ?inchikey }}
  OPTIONAL {{ ?compound wdt:P233 ?smiles }}
  OPTIONAL {{ ?stmt prov:wasDerivedFrom/pr:P248 ?ref }}
}} LIMIT {limit * 4}
"""
    )
    rows: list[dict[str, Any]] = data["results"]["bindings"]

    if not rows:
        return no_match_result(
            SOURCE,
            args,
            f"https://www.wikidata.org/wiki/Special:Search?search={quote(taxon)}",
            [
                *warnings,
                "LOTUS has no record for this taxon, or the name differs from the accepted "
                "scientific name used in Wikidata",
            ],
        )

    # One compound can have several references; group rows by compound.
    by_compound: dict[str, dict[str, Any]] = {}
    taxon_qid = ""
    for row in rows:
        cid = _qid(row.get("compound", {}).get("value", ""))
        if not cid:
            continue
        taxon_qid = taxon_qid or _qid(row.get("taxonItem", {}).get("value", ""))
        entry = by_compound.setdefault(
            cid,
            {
                "inchikey": row.get("inchikey", {}).get("value"),
                "smiles": row.get("smiles", {}).get("value"),
                "refs": [],
            },
        )
        rid = _qid(row.get("ref", {}).get("value", ""))
        if rid and rid not in entry["refs"]:
            entry["refs"].append(rid)

    compound_ids = list(by_compound)[:limit]

    # Only limit*4 rows were requested, so len(by_compound) counts the compounds
    # in the rows that came back, not the true total for the taxon.
    hit_sparql_cap = len(rows) >= limit * 4
    truncated = len(by_compound) > len(compound_ids) or hit_sparql_cap
    total_known = None if hit_sparql_cap else len(by_compound)
    if hit_sparql_cap:
        warnings.append(
            f"only the first {len(rows)} pairs were fetched; the true number of compounds "
            "for this taxon is unknown (counting needs another rate-limited SPARQL query)"
        )

    # Step 2: labels and reference details, which SPARQL cannot provide.
    needed = compound_ids + [r for cid in compound_ids for r in by_compound[cid]["refs"]]
    entities = _fetch_entities(needed)

    raw_path = archive(
        SOURCE,
        "search_by_taxon",
        args,
        {"sparql": data, "entities": {q: _trim_entity(e) for q, e in entities.items()}},
        [sparql_url, API_EP],
    )

    records = []
    no_citation = 0
    for cid in compound_ids:
        entry = by_compound[cid]
        citations = [
            _build_citation(rid, entities[rid]) for rid in entry["refs"] if rid in entities
        ]
        if not citations:
            no_citation += 1
        records.append(
            OccurrenceRecord(
                wikidata_id=cid,
                compound_name=_label(entities.get(cid, {})),
                inchikey=entry["inchikey"],
                smiles=entry["smiles"],
                taxon_name=taxon,
                taxon_wikidata_id=taxon_qid,
                references=citations,
                reference_count=len(citations),
                source_url=f"{ENTITY_URL}{cid}",
            ).model_dump()
        )

    if no_citation:
        warnings.append(
            f"{no_citation}/{len(records)} pairs came without a literature reference; "
            "do not treat them as equivalent to referenced pairs"
        )
    missing_structure = sum(1 for r in records if not r["inchikey"])
    if missing_structure:
        warnings.append(
            f"{missing_structure}/{len(records)} compounds have no InChIKey and cannot be "
            "deduplicated by structure"
        )

    return ToolResult(
        status=Status.OK,
        source=SOURCE,
        query=args,
        records=records,
        source_url=f"{ENTITY_URL}{taxon_qid}" if taxon_qid else None,
        truncated=truncated,
        total_available=total_known,
        warnings=warnings,
        raw_path=raw_path,
    )


def _fetch_occurrences(args: dict[str, Any]) -> ToolResult:
    inchikey: str = args["inchikey"]
    limit, limit_warnings = clamp_limit(SOURCE, args["limit"])

    if not INCHIKEY_RE.match(inchikey):
        return error_result(
            SOURCE,
            args,
            "not a full InChIKey (expected 27 characters such as "
            "BLUAFEHZUWYNDE-NNWCWBAJSA-N); no request was sent",
        )

    # One extra row tells whether more taxa exist than were asked for.
    data, sparql_url = _sparql(
        f"""
SELECT ?compound ?taxonItem ?taxonName WHERE {{
  ?compound wdt:P235 "{inchikey}" .
  ?compound p:P703 ?stmt .
  ?stmt ps:P703 ?taxonItem .
  ?taxonItem wdt:P225 ?taxonName .
}} LIMIT {limit + 1}
"""
    )
    rows: list[dict[str, Any]] = data["results"]["bindings"]
    search_url = f"https://www.wikidata.org/wiki/Special:Search?search={quote(inchikey)}"

    if not rows:
        return no_match_result(SOURCE, args, search_url)

    compound_qid = _qid(rows[0].get("compound", {}).get("value", ""))
    entities = _fetch_entities([compound_qid]) if compound_qid else {}
    name = _label(entities.get(compound_qid, {})) if compound_qid else None

    raw_path = archive(
        SOURCE,
        "search_occurrences",
        args,
        {"sparql": data, "entities": {q: _trim_entity(e) for q, e in entities.items()}},
        [sparql_url, API_EP],
    )

    truncated = len(rows) > limit
    records = []
    for row in rows[:limit]:
        tqid = _qid(row.get("taxonItem", {}).get("value", ""))
        records.append(
            OccurrenceRecord(
                wikidata_id=compound_qid,
                compound_name=name,
                inchikey=inchikey,
                taxon_name=row.get("taxonName", {}).get("value"),
                taxon_wikidata_id=tqid,
                source_url=f"{ENTITY_URL}{tqid}",
            ).model_dump()
        )

    return ToolResult(
        status=Status.OK,
        source=SOURCE,
        query=args,
        records=records,
        source_url=f"{ENTITY_URL}{compound_qid}" if compound_qid else search_url,
        truncated=truncated,
        total_available=None if truncated else len(records),
        warnings=[
            *limit_warnings,
            "this query lists taxa only; literature references per pair are returned by "
            "search_by_taxon",
        ],
        raw_path=raw_path,
    )


def search_by_taxon(taxon: str, limit: int = 20, session_id: str | None = None) -> ToolResult:
    """List natural products reported in a taxon, with literature references.

    Args:
        taxon: Scientific name as accepted in Wikidata (e.g. ``"Artemisia annua"``).
            Common names and non-Latin names are not recognised.
        limit: Maximum number of compounds to return.
        session_id: Optional session identifier for the call log.
    """
    args = {"taxon": taxon.strip(), "limit": limit}
    return run_connector(
        source=SOURCE,
        func_name="search_by_taxon",
        args=args,
        fetch=lambda: _fetch_by_taxon(args),
        session_id=session_id,
    )


def search_occurrences(inchikey: str, limit: int = 20, session_id: str | None = None) -> ToolResult:
    """List the taxa in which a compound has been reported.

    Args:
        inchikey: Full InChIKey (27 characters, e.g. ``"BLUAFEHZUWYNDE-NNWCWBAJSA-N"``).
        limit: Maximum number of taxa to return.
        session_id: Optional session identifier for the call log.
    """
    args = {"inchikey": inchikey.strip().upper(), "limit": limit}
    return run_connector(
        source=SOURCE,
        func_name="search_occurrences",
        args=args,
        fetch=lambda: _fetch_occurrences(args),
        session_id=session_id,
    )
