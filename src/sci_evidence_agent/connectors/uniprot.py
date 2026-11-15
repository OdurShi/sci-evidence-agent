"""UniProt connector (UniProtKB REST API).

Only Swiss-Prot (``reviewed:true``) is searched by default. TrEMBL is two
orders of magnitude larger and unreviewed; it must be requested explicitly.

Upstream behaviour this connector depends on:

* The true number of hits is only in the ``X-Total-Results`` response header;
  ``len(results)`` is capped by ``size``.
* ``size`` above 500 is answered with HTTP 400.
* The release number and date are only in response headers.
* A deleted or merged accession is answered with HTTP 200 and
  ``entryType = "Inactive"`` (no protein data). That is ``no_match``.
* A secondary accession is redirected to the entry that absorbed it.
* A malformed query (unknown field, bad syntax) is answered with HTTP 400.
"""

from __future__ import annotations

import re
from typing import Any

from sci_evidence_agent.connectors.base import archive, clamp_limit, run_connector
from sci_evidence_agent.http import get_client
from sci_evidence_agent.schemas import ProteinRecord, Status, ToolResult, no_match_result

SOURCE = "UniProt"
BASE = "https://rest.uniprot.org/uniprotkb"
WEB = "https://www.uniprot.org/uniprotkb"

FIELDS = "accession,protein_name,gene_names,organism_name,organism_id,length,reviewed"

# Official accession format, e.g. P00533, Q9Y6K9, A0A024R161.
ACCESSION_RE = re.compile(
    r"^[OPQ][0-9][A-Z0-9]{3}[0-9]$|^[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}$"
)


def _parse_entry(entry: dict[str, Any]) -> ProteinRecord:
    desc = entry.get("proteinDescription", {})
    rec_name = desc.get("recommendedName", {}).get("fullName", {}).get("value")
    if not rec_name:
        submitted = desc.get("submissionNames") or []
        rec_name = submitted[0].get("fullName", {}).get("value") if submitted else None

    organism = entry.get("organism", {})
    entry_type = entry.get("entryType", "").lower()

    return ProteinRecord(
        accession=entry.get("primaryAccession", ""),
        protein_name=rec_name,
        gene_names=[
            g.get("geneName", {}).get("value") for g in entry.get("genes", []) if g.get("geneName")
        ],
        organism=organism.get("scientificName"),
        taxonomy_id=organism.get("taxonId"),
        reviewed="reviewed" in entry_type and "unreviewed" not in entry_type,
        sequence_length=entry.get("sequence", {}).get("length"),
        source_url=f"{WEB}/{entry.get('primaryAccession')}",
    )


def _fetch(args: dict[str, Any]) -> ToolResult:
    query: str = args["query"]
    organism_id: int | None = args["organism_id"]
    reviewed_only: bool = args["reviewed_only"]
    limit, limit_warnings = clamp_limit(SOURCE, args["limit"])
    client = get_client()

    # Something that looks like an accession is fetched directly.
    if ACCESSION_RE.match(query.upper()):
        accession = query.upper()
        resp = client.get_json(SOURCE, f"{BASE}/{accession}.json", ok_404=True)
        if resp.status == 404 or resp.data is None:
            return no_match_result(SOURCE, args, resp.url)

        if resp.data.get("entryType") == "Inactive":
            reason = resp.data.get("inactiveReason") or {}
            detail = reason.get("inactiveReasonType", "unknown reason")
            merged = reason.get("mergeDemergeTo") or []
            note = f"{accession} is an inactive UniProtKB entry ({detail})"
            if merged:
                note += f"; see {', '.join(merged)}"
            return no_match_result(
                SOURCE,
                args,
                f"{WEB}/{accession}",
                [*limit_warnings, note, *_release_note(resp.headers)],
            )

        raw_path = archive(SOURCE, "search_protein", args, resp.data, [resp.url])
        record = _parse_entry(resp.data)
        warnings = list(limit_warnings)
        if record.accession and record.accession != accession:
            warnings.append(
                f"{accession} is a secondary accession; the current entry is {record.accession}"
            )
        return ToolResult(
            status=Status.OK,
            source=SOURCE,
            query=args,
            records=[record.model_dump()],
            source_url=record.source_url,
            total_available=1,
            warnings=warnings + _release_note(resp.headers),
            raw_path=raw_path,
        )

    # Otherwise build a search expression. The user's query is parenthesised so
    # that an OR inside it cannot swallow the filters that follow.
    filters = []
    if organism_id is not None:
        filters.append(f"organism_id:{organism_id}")
    if reviewed_only:
        filters.append("reviewed:true")
    expr = " AND ".join([f"({query})", *filters]) if filters else query

    resp = client.get_json(
        SOURCE,
        f"{BASE}/search",
        {"query": expr, "fields": FIELDS, "format": "json", "size": limit},
    )

    hits: list[dict[str, Any]] = resp.data.get("results", [])
    if not hits:
        warnings = list(limit_warnings)
        if reviewed_only:
            warnings.append(
                "only Swiss-Prot was searched; the name may exist only in unreviewed TrEMBL"
            )
        return no_match_result(SOURCE, args, resp.url, warnings + _release_note(resp.headers))

    raw_path = archive(SOURCE, "search_protein", args, resp.data, [resp.url])
    records = [_parse_entry(h).model_dump() for h in hits[:limit]]

    # The real total is only in the header; len(hits) is capped by ``size`` and
    # would make ``truncated`` always False.
    total = _header_int(resp.headers, "x-total-results", default=len(hits))

    warnings = list(limit_warnings)
    if reviewed_only:
        warnings.append("only Swiss-Prot (manually reviewed) entries are included")
    if organism_id is None and len({r["taxonomy_id"] for r in records}) > 1:
        warnings.append(
            "results span several organisms; restrict with organism_id if needed (human is 9606)"
        )
    warnings += _release_note(resp.headers)

    status = Status.AMBIGUOUS if len(records) > 1 else Status.OK

    return ToolResult(
        status=status,
        source=SOURCE,
        query=args,
        records=records,
        source_url=records[0]["source_url"],
        truncated=total > len(records),
        total_available=total,
        warnings=warnings,
        raw_path=raw_path,
    )


def _header_int(headers: dict[str, str], name: str, default: int) -> int:
    try:
        return int(headers.get(name, ""))
    except (TypeError, ValueError):
        return default


def _release_note(headers: dict[str, str]) -> list[str]:
    """The UniProt release is provenance; it only appears in response headers."""
    release = headers.get("x-uniprot-release")
    date = headers.get("x-uniprot-release-date")
    if not release:
        return []
    return [f"UniProt release {release}" + (f" ({date})" if date else "")]


def search_protein(
    query: str,
    organism_id: int | None = None,
    reviewed_only: bool = True,
    limit: int = 10,
    session_id: str | None = None,
) -> ToolResult:
    """Search UniProtKB by protein name, gene name or accession.

    Args:
        query: Protein name, gene name (e.g. ``"gene:EGFR"``) or accession (e.g. ``"P00533"``).
        organism_id: NCBI taxonomy ID; human is 9606. None means all organisms.
        reviewed_only: Restrict to Swiss-Prot entries. Default True.
        limit: Maximum number of records to return.
        session_id: Optional session identifier for the call log.
    """
    args = {
        "query": query.strip(),
        "organism_id": organism_id,
        "reviewed_only": reviewed_only,
        "limit": limit,
    }
    return run_connector(
        source=SOURCE,
        func_name="search_protein",
        args=args,
        fetch=lambda: _fetch(args),
        session_id=session_id,
    )
