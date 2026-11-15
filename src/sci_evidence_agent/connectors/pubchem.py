"""PubChem connector (PUG REST).

Upstream behaviour this connector depends on (checked against the live API):

* An unknown name is answered with HTTP 404 and ``Fault.Code =
  PUGREST.NotFound``. That is ``no_match``, not ``error``.
* A numeric CID that is syntactically valid but has no live compound record
  is answered with HTTP 200 and a row that contains only ``CID``. That is
  also ``no_match``; treating it as a hit would create an empty record.
* The property is called ``SMILES``; the former ``CanonicalSMILES`` name no
  longer works.
* Every response carries ``X-Throttling-Control`` with the current load
  level (Green / Yellow / Red / Black).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from sci_evidence_agent.connectors.base import archive, clamp_limit, run_connector
from sci_evidence_agent.http import get_client
from sci_evidence_agent.schemas import CompoundRecord, Status, ToolResult, no_match_result

SOURCE = "PubChem"
BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
WEB = "https://pubchem.ncbi.nlm.nih.gov/compound"

PROPERTIES = "MolecularFormula,SMILES,InChIKey,MolecularWeight,IUPACName"


def _fetch(args: dict[str, Any]) -> ToolResult:
    query: str = args["query"]
    limit, limit_warnings = clamp_limit(SOURCE, args["limit"])

    # A purely numeric query is a CID; anything else is a name.
    if query.isdigit():
        path = f"{BASE}/compound/cid/{query}/property/{PROPERTIES}/JSON"
    else:
        path = f"{BASE}/compound/name/{quote(query, safe='')}/property/{PROPERTIES}/JSON"

    resp = get_client().get_json(SOURCE, path, ok_404=True)

    if resp.status == 404 or resp.data is None:
        return no_match_result(SOURCE, args, resp.url)

    rows: list[dict[str, Any]] = resp.data.get("PropertyTable", {}).get("Properties", [])
    # A row with nothing but the CID means "no live compound behind this number".
    live = [row for row in rows if any(key != "CID" for key in row)]
    if not live:
        warnings = (
            [f"CID {query} is a valid number but PubChem holds no compound record for it"]
            if rows
            else []
        )
        return no_match_result(SOURCE, args, resp.url, warnings)

    raw_path = archive(SOURCE, "search_compound", args, resp.data, [resp.url])

    truncated = len(live) > limit
    records = []
    for row in live[:limit]:
        cid = row.get("CID")
        mw = row.get("MolecularWeight")
        records.append(
            CompoundRecord(
                source_db=SOURCE,
                source_id=str(cid),
                name=row.get("IUPACName"),
                molecular_formula=row.get("MolecularFormula"),
                smiles=row.get("SMILES"),
                inchikey=row.get("InChIKey"),
                # PubChem returns the molecular weight as a string.
                molecular_weight=float(mw) if mw not in (None, "") else None,
                source_url=f"{WEB}/{cid}",
            ).model_dump()
        )

    warnings: list[str] = list(limit_warnings)
    if any(r["name"] is None for r in records):
        warnings.append("some records have no IUPAC name")

    # Report the load level before it turns into HTTP 503.
    throttle = resp.headers.get("x-throttling-control", "")
    if any(level in throttle for level in ("Yellow", "Red", "Black")):
        warnings.append(f"PubChem throttling warning: {throttle}")

    # One name resolving to several CIDs is an ambiguity for the caller to settle.
    status = Status.AMBIGUOUS if len(records) > 1 else Status.OK

    return ToolResult(
        status=status,
        source=SOURCE,
        query=args,
        records=records,
        source_url=records[0]["source_url"] if records else resp.url,
        truncated=truncated,
        total_available=len(live),
        warnings=warnings,
        raw_path=raw_path,
    )


def search_compound(query: str, limit: int = 10, session_id: str | None = None) -> ToolResult:
    """Look up basic chemical information by compound name or PubChem CID.

    Args:
        query: Compound name (e.g. ``"aspirin"``) or a numeric CID (e.g. ``"2244"``).
        limit: Maximum number of records to return.
        session_id: Optional session identifier for the call log.
    """
    args = {"query": query.strip(), "limit": limit}
    return run_connector(
        source=SOURCE,
        func_name="search_compound",
        args=args,
        fetch=lambda: _fetch(args),
        session_id=session_id,
    )
