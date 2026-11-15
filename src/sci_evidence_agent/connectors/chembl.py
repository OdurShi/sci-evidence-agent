"""ChEMBL connector (web services).

Two capabilities: search molecules by name or ChEMBL ID, and fetch the
bioactivity records of a molecule.

Upstream behaviour this connector depends on:

* ``standard_units`` can be null (dimensionless measurements such as
  ``Log K'``) and ``standard_value`` is a string. Nulls are kept as they are.
* ``limit`` is silently capped at 1000.
* An unknown ChEMBL ID is answered with HTTP 404 and an HTML body.

Licence note: ChEMBL data are CC BY-SA 3.0 (share-alike). This package only
queries and caches; publishing a dataset built from ChEMBL records requires a
compatible licence for the whole dataset.
"""

from __future__ import annotations

from typing import Any

from sci_evidence_agent.connectors.base import archive, clamp_limit, run_connector
from sci_evidence_agent.http import get_client
from sci_evidence_agent.schemas import (
    BioactivityRecord,
    CompoundRecord,
    Status,
    ToolResult,
    no_match_result,
)

SOURCE = "ChEMBL"
BASE = "https://www.ebi.ac.uk/chembl/api/data"
WEB = "https://www.ebi.ac.uk/chembl/compound_report_card"
ASSAY_WEB = "https://www.ebi.ac.uk/chembl/assay_report_card"


def _molecule_url(chembl_id: str) -> str:
    return f"{WEB}/{chembl_id}/"


def _is_chembl_id(text: str) -> bool:
    return text.upper().startswith("CHEMBL") and text[6:].isdigit()


def _fetch_molecule(args: dict[str, Any]) -> ToolResult:
    query: str = args["query"]
    limit, limit_warnings = clamp_limit(SOURCE, args["limit"])
    client = get_client()

    # A ChEMBL ID is fetched directly; no fuzzy search needed.
    if _is_chembl_id(query):
        resp = client.get_json(SOURCE, f"{BASE}/molecule/{query.upper()}.json", ok_404=True)
        if resp.status == 404 or resp.data is None:
            return no_match_result(SOURCE, args, resp.url)
        molecules = [resp.data]
        total = 1
    else:
        resp = client.get_json(SOURCE, f"{BASE}/molecule/search.json", {"q": query, "limit": limit})
        molecules = resp.data.get("molecules", [])
        total = resp.data.get("page_meta", {}).get("total_count", len(molecules))
        if not molecules:
            return no_match_result(SOURCE, args, resp.url)

    raw_path = archive(SOURCE, "search_chembl_molecule", args, resp.data, [resp.url])

    records = []
    for m in molecules[:limit]:
        props = m.get("molecule_properties") or {}
        structs = m.get("molecule_structures") or {}
        cid = m.get("molecule_chembl_id")
        mw = props.get("full_mwt")
        records.append(
            CompoundRecord(
                source_db=SOURCE,
                source_id=cid,
                name=m.get("pref_name"),
                molecular_formula=props.get("full_molformula"),
                smiles=structs.get("canonical_smiles"),
                inchikey=structs.get("standard_inchi_key"),
                molecular_weight=float(mw) if mw not in (None, "") else None,
                source_url=_molecule_url(cid),
            ).model_dump()
        )

    warnings: list[str] = list(limit_warnings)
    if any(r["name"] is None for r in records):
        warnings.append("some records have no pref_name (common in ChEMBL)")

    # An exact match on the preferred name settles the query; otherwise several
    # candidates are an ambiguity.
    exact = [r for r in records if (r["name"] or "").lower() == query.lower()]
    if exact:
        status = Status.OK
        records = exact + [r for r in records if r not in exact]
    else:
        status = Status.AMBIGUOUS if len(records) > 1 else Status.OK

    return ToolResult(
        status=status,
        source=SOURCE,
        query=args,
        records=records,
        source_url=records[0]["source_url"] if records else resp.url,
        truncated=total > len(records),
        total_available=total,
        warnings=warnings,
        raw_path=raw_path,
    )


def _fetch_activities(args: dict[str, Any]) -> ToolResult:
    chembl_id: str = args["chembl_id"]
    limit, limit_warnings = clamp_limit(SOURCE, args["limit"])

    resp = get_client().get_json(
        SOURCE,
        f"{BASE}/activity.json",
        {"molecule_chembl_id": chembl_id, "limit": limit},
        ok_404=True,
    )
    if resp.status == 404 or resp.data is None:
        return no_match_result(SOURCE, args, resp.url)

    activities: list[dict[str, Any]] = resp.data.get("activities", [])
    if not activities:
        return no_match_result(SOURCE, args, resp.url)

    raw_path = archive(SOURCE, "get_bioactivities", args, resp.data, [resp.url])
    total = resp.data.get("page_meta", {}).get("total_count", len(activities))

    records = []
    missing_units = 0
    for a in activities[:limit]:
        units = a.get("standard_units")
        if units is None:
            missing_units += 1
        assay_id = a.get("assay_chembl_id")
        records.append(
            BioactivityRecord(
                activity_id=str(a.get("activity_id")) if a.get("activity_id") else None,
                molecule_chembl_id=a.get("molecule_chembl_id"),
                target_chembl_id=a.get("target_chembl_id"),
                target_name=a.get("target_pref_name"),
                assay_chembl_id=assay_id,
                assay_type=a.get("assay_type"),
                standard_type=a.get("standard_type"),
                # Values arrive as strings; an unparsable value stays None.
                standard_value=_to_float(a.get("standard_value")),
                standard_units=units,
                standard_relation=a.get("standard_relation"),
                source_url=f"{ASSAY_WEB}/{assay_id}/" if assay_id else None,
            ).model_dump()
        )

    warnings: list[str] = list(limit_warnings)
    if missing_units:
        warnings.append(
            f"{missing_units}/{len(records)} records have no units "
            "(dimensionless measurements; this is normal)"
        )

    return ToolResult(
        status=Status.OK,
        source=SOURCE,
        query=args,
        records=records,
        source_url=_molecule_url(chembl_id),
        truncated=total > len(records),
        total_available=total,
        warnings=warnings,
        raw_path=raw_path,
    )


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def search_chembl_molecule(
    query: str, limit: int = 10, session_id: str | None = None
) -> ToolResult:
    """Search ChEMBL molecules by name or ChEMBL ID.

    Args:
        query: Molecule name (e.g. ``"aspirin"``) or ChEMBL ID (e.g. ``"CHEMBL25"``).
        limit: Maximum number of records to return.
        session_id: Optional session identifier for the call log.
    """
    args = {"query": query.strip(), "limit": limit}
    return run_connector(
        source=SOURCE,
        func_name="search_chembl_molecule",
        args=args,
        fetch=lambda: _fetch_molecule(args),
        session_id=session_id,
    )


def get_bioactivities(chembl_id: str, limit: int = 20, session_id: str | None = None) -> ToolResult:
    """Fetch the bioactivity records of one molecule.

    Args:
        chembl_id: ChEMBL ID of the molecule (e.g. ``"CHEMBL25"``).
        limit: Maximum number of activity records to return.
        session_id: Optional session identifier for the call log.
    """
    args = {"chembl_id": chembl_id.strip().upper(), "limit": limit}
    return run_connector(
        source=SOURCE,
        func_name="get_bioactivities",
        args=args,
        fetch=lambda: _fetch_activities(args),
        session_id=session_id,
    )
