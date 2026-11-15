"""Template report for the offline planner. This is not model output.

The reporter turns the compact tool results (the same JSON a model would
receive) into Markdown. It adds nothing: every identifier and value it prints
is copied from a tool result, and the four result states are worded
differently on purpose so that a failed request can never read like
"no data".
"""

from __future__ import annotations

from typing import Any

from sci_evidence_agent.planning import Plan

MAX_RECORDS = 10

DISCLAIMER = (
    "> Note: this text was assembled by the offline keyword planner from templates. "
    "It is not model output. Tools were selected by keyword rules; the database "
    "queries and the records below are real."
)

NO_ENTITY = (
    "No entity to look up was recognised in the question. The offline planner only reads "
    'identifiers, quoted phrases and ASCII names (for example aspirin, EGFR, "protein '
    'kinase C"); it does not translate names and does not understand complex requests.'
)

NO_QUERY = "No database query was produced for this question."

STATE_TEXT = {
    "no_match": "No matching record. The query was valid; this is not a failure.",
    "error": "Request failed. This is a failed request, not an absence of data.",
    "ambiguous": "Several candidates matched and none was selected automatically. "
    "Confirm which one is meant:",
}


def _join(parts: list[str | None]) -> str:
    return " · ".join(p for p in parts if p)


def _link(record: dict[str, Any]) -> str | None:
    url = record.get("source_url")
    return f"[source]({url})" if url else None


def _compound(r: dict[str, Any]) -> str:
    ident = f"CID {r['source_id']}" if r.get("source_db") == "PubChem" else str(r.get("source_id"))
    mw = r.get("molecular_weight")
    return _join(
        [
            f"`{ident}`",
            r.get("name"),
            r.get("molecular_formula"),
            f"MW {mw:g}" if isinstance(mw, (int, float)) else None,
            f"InChIKey {r['inchikey']}" if r.get("inchikey") else None,
            f"SMILES `{r['smiles']}`" if r.get("smiles") else None,
            _link(r),
        ]
    )


def _bioactivity(r: dict[str, Any]) -> str:
    value = r.get("standard_value")
    measurement = " ".join(
        str(x)
        for x in (
            r.get("standard_type") or "measurement",
            r.get("standard_relation"),
            f"{value:g}" if isinstance(value, (int, float)) else "(value missing)",
            r.get("standard_units") or "(no units)",
        )
        if x
    )
    target = r.get("target_chembl_id")
    if target and r.get("target_name"):
        target = f"{target} ({r['target_name']})"
    return _join(
        [
            f"`{r.get('molecule_chembl_id')}`" if r.get("molecule_chembl_id") else None,
            f"target {target}" if target else "target not given",
            f"assay {r['assay_chembl_id']}" if r.get("assay_chembl_id") else None,
            f"assay type {r['assay_type']}" if r.get("assay_type") else None,
            measurement,
            _link(r),
        ]
    )


def _protein(r: dict[str, Any]) -> str:
    organism = r.get("organism")
    if organism and r.get("taxonomy_id"):
        organism = f"{organism} (taxonomy ID {r['taxonomy_id']})"
    reviewed = {True: "Swiss-Prot (reviewed)", False: "TrEMBL (unreviewed)"}.get(r.get("reviewed"))
    return _join(
        [
            f"`{r.get('accession')}`",
            r.get("protein_name"),
            f"genes: {', '.join(r['gene_names'])}" if r.get("gene_names") else None,
            organism,
            f"{r['sequence_length']} aa" if r.get("sequence_length") else None,
            reviewed,
            _link(r),
        ]
    )


def _literature(r: dict[str, Any]) -> str:
    authors = r.get("authors") or []
    byline = None
    if authors:
        byline = authors[0] + (" et al." if len(authors) > 1 else "")
    return _join(
        [
            f"PMID {r.get('pmid')}",
            r.get("title"),
            byline,
            r.get("journal"),
            r.get("pub_date"),
            f"DOI {r['doi']}" if r.get("doi") else "no DOI",
            _link(r),
        ]
    )


def _occurrence(r: dict[str, Any]) -> str:
    refs = r.get("references") or []
    references = None
    if refs:
        shown = []
        for ref in refs[:2]:
            bits = [
                f"DOI {ref['doi']}" if ref.get("doi") else None,
                f"PMID {ref['pmid']}" if ref.get("pmid") else None,
            ]
            shown.append(" / ".join(b for b in bits if b) or f"Wikidata {ref.get('wikidata_id')}")
        more = f" (+{len(refs) - 2} more)" if len(refs) > 2 else ""
        references = "references: " + "; ".join(shown) + more
    taxon = r.get("taxon_name")
    if taxon and r.get("taxon_wikidata_id"):
        taxon = f"{taxon} (Wikidata {r['taxon_wikidata_id']})"
    return _join(
        [
            r.get("compound_name") or "unnamed compound",
            f"Wikidata {r.get('wikidata_id')}",
            f"InChIKey {r['inchikey']}" if r.get("inchikey") else None,
            f"taxon: {taxon}" if taxon else None,
            references,
            _link(r),
        ]
    )


def format_record(record: dict[str, Any]) -> str:
    """One Markdown line for a record of any type."""
    if "accession" in record:
        return _protein(record)
    if "pmid" in record and "wikidata_id" not in record:
        return _literature(record)
    if "wikidata_id" in record:
        return _occurrence(record)
    if "assay_chembl_id" in record or "standard_type" in record or "activity_id" in record:
        return _bioactivity(record)
    return _compound(record)


def render_report(
    plan: Plan | None,
    observations: list[tuple[str, dict[str, Any]]],
    skipped: list[str] | None = None,
    max_records: int = MAX_RECORDS,
) -> str:
    """Render the final answer of the offline planner.

    Args:
        plan: The plan that was executed (for the recognised entities and notes).
        observations: ``(tool name, compact result payload)`` in call order.
        skipped: Explanations for planned calls that were not issued.
        max_records: Number of records listed per tool result.
    """
    lines = [DISCLAIMER, ""]

    if not observations:
        lines.append(NO_ENTITY if plan is None or plan.entities.is_empty() else NO_QUERY)
        lines += [f"- {note}" for note in (skipped or [])]
        return "\n".join(lines)

    if plan is not None:
        labels = plan.entities.labels()
        if labels:
            lines.append("Recognised in the question: " + ", ".join(f"`{x}`" for x in labels))
        lines += [f"- {note}" for note in plan.notes]

    for tool, payload in observations:
        source = payload.get("source", "?")
        status = payload.get("status", "?")
        lines.append("")
        # For a failure the detail goes on its own line instead of into the heading.
        heading = "request failed" if status == "error" else payload.get("summary", "")
        lines.append(f"**{source}** · `{tool}` · {status}: {heading}")

        if status in STATE_TEXT:
            lines.append(STATE_TEXT[status])
        if payload.get("error"):
            lines.append(f"- Error: {payload['error']}")

        records = payload.get("records") or []
        if status in ("ok", "ambiguous"):
            lines += [f"- {format_record(r)}" for r in records[:max_records]]
            hidden = len(records) - max_records
            if hidden > 0:
                lines.append(f"- ... {hidden} more returned records are not listed here")

        if payload.get("truncated"):
            total = payload.get("total_available")
            lines.append(
                f"- Truncated: {len(records)} of {total} records available upstream."
                if total is not None
                else f"- Truncated: {len(records)} records returned; the upstream total is unknown."
            )
        lines += [f"- Note: {w}" for w in payload.get("warnings", [])]
        if payload.get("source_url"):
            lines.append(f"- Check in the database: {payload['source_url']}")

    for note in skipped or []:
        lines.append("")
        lines.append(f"Skipped: {note}")

    return "\n".join(lines)
