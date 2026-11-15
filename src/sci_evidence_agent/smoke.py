"""Connectivity smoke test: are the upstream services reachable and shaped as expected?

Each check hits a raw endpoint, not a connector, so a failure here separates
"the network or the API changed" from "our field mapping is wrong".

By default requests go through this package's HTTP layer (timeout, rate
limit, retries), because that is what production calls use and EBI is
occasionally slow. ``raw=True`` bypasses the layer with a bare ``httpx.get``;
use it when the HTTP layer itself is under suspicion.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from sci_evidence_agent import config
from sci_evidence_agent.http import TIMEOUT, UpstreamError, get_client

Check = tuple[str, bool, str]


def _get(
    source: str,
    url: str,
    params: dict[str, Any] | None,
    raw: bool,
    ok_404: bool = False,
) -> tuple[int, Any, dict[str, str]]:
    if not raw:
        resp = get_client().get_json(source, url, params, ok_404=ok_404)
        return resp.status, resp.data, resp.headers
    resp = httpx.get(
        url,
        params=params,
        timeout=TIMEOUT,
        headers={"User-Agent": config.user_agent(), "Accept": "application/json"},
        follow_redirects=True,
    )
    headers = {k.lower(): v for k, v in resp.headers.items()}
    if resp.status_code == 404 and ok_404:
        return 404, None, headers
    if resp.status_code != 200:
        raise UpstreamError(f"HTTP {resp.status_code}")
    return 200, resp.json(), headers


def check_pubchem(raw: bool) -> Check:
    """Name lookup; also confirms that the property is still called SMILES."""
    url = (
        "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/aspirin"
        "/property/MolecularFormula,SMILES,InChIKey/JSON"
    )
    _, data, headers = _get("PubChem", url, None, raw)
    row = data["PropertyTable"]["Properties"][0]
    if "SMILES" not in row:
        return "PubChem", False, f"property names changed; got {sorted(row)}"
    throttle = headers.get("x-throttling-control", "")
    return "PubChem", True, f"CID={row['CID']} fields={sorted(row)} | throttle: {throttle}"


def check_pubchem_no_match(raw: bool) -> Check:
    """An unknown name must be HTTP 404; the connector maps it to no_match."""
    url = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/zzzznotacompound/cids/JSON"
    code, data, _ = _get("PubChem", url, None, raw, ok_404=True)
    return "PubChem no_match", code == 404 and data is None, f"HTTP {code} (expected 404)"


def check_chembl_molecule(raw: bool) -> Check:
    _, data, _ = _get(
        "ChEMBL",
        "https://www.ebi.ac.uk/chembl/api/data/molecule/search.json",
        {"q": "aspirin", "limit": 3},
        raw,
    )
    molecules = data.get("molecules", [])
    if not molecules:
        return "ChEMBL molecule", False, f"HTTP 200 without molecules: {list(data)}"
    first = molecules[0]
    total = data.get("page_meta", {}).get("total_count")
    return (
        "ChEMBL molecule",
        True,
        f"{len(molecules)} hits, first {first.get('molecule_chembl_id')} "
        f"pref_name={first.get('pref_name')} total={total}",
    )


def check_chembl_activity(raw: bool) -> Check:
    """Activity records: field names and nullability."""
    _, data, _ = _get(
        "ChEMBL",
        "https://www.ebi.ac.uk/chembl/api/data/activity.json",
        {"molecule_chembl_id": "CHEMBL25", "limit": 3},
        raw,
    )
    activities = data.get("activities", [])
    if not activities:
        return "ChEMBL activity", False, "HTTP 200 with an empty activity list"
    wanted = ("standard_type", "standard_value", "standard_units", "target_chembl_id")
    present = {k: activities[0].get(k) for k in wanted}
    return "ChEMBL activity", True, f"{len(activities)} records, {json.dumps(present)}"


def check_uniprot(raw: bool) -> Check:
    """Gene + organism search; the total must be in the response header."""
    _, data, headers = _get(
        "UniProt",
        "https://rest.uniprot.org/uniprotkb/search",
        {
            "query": "(gene:EGFR) AND organism_id:9606 AND reviewed:true",
            "fields": "accession,protein_name,gene_names,organism_name",
            "format": "json",
            "size": 3,
        },
        raw,
    )
    hits = data.get("results", [])
    if not hits:
        return "UniProt", False, "HTTP 200 with an empty result list"
    return (
        "UniProt",
        True,
        f"{len(hits)} hit(s), first {hits[0].get('primaryAccession')} | "
        f"X-Total-Results={headers.get('x-total-results', '?')} "
        f"release={headers.get('x-uniprot-release', '?')}",
    )


def check_pubmed(raw: bool) -> Check:
    """esearch connectivity and the rate-limit headroom."""
    _, data, headers = _get(
        "PubMed",
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        {
            "db": "pubmed",
            "term": "aspirin AND cyclooxygenase",
            "retmode": "json",
            "retmax": 3,
            "tool": "sci-evidence-agent",
        },
        raw,
    )
    block = data.get("esearchresult", {})
    ids = block.get("idlist", [])
    if not ids:
        return "PubMed", False, f"HTTP 200 without PMIDs: {sorted(block)}"
    return (
        "PubMed",
        True,
        f"{block.get('count')} hits, first PMID {ids[0]} | rate limit "
        f"{headers.get('x-ratelimit-remaining', '?')}/{headers.get('x-ratelimit-limit', '?')}",
    )


CHECKS: tuple[Callable[[bool], Check], ...] = (
    check_pubchem,
    check_pubchem_no_match,
    check_chembl_molecule,
    check_chembl_activity,
    check_uniprot,
    check_pubmed,
)


def run_smoke(raw: bool = False, echo: Callable[[str], None] = print) -> list[Check]:
    """Run all checks. LOTUS/Wikidata is left out because of its strict usage limit."""
    mode = "bare httpx (no retries)" if raw else "package HTTP layer (timeout, limit, retries)"
    echo(f"Smoke test via {mode}")
    results: list[Check] = []
    started = time.monotonic()
    for check in CHECKS:
        try:
            result = check(raw)
        except Exception as exc:  # every failure is reported as a failed check
            label = check.__name__.replace("check_", "")
            result = (label, False, f"{type(exc).__name__}: {exc}")
        results.append(result)
        echo(f"  [{'pass' if result[1] else 'FAIL'}] {result[0]}: {result[2]}")
    passed = sum(1 for _, ok, _ in results if ok)
    echo(f"{passed}/{len(results)} checks passed in {time.monotonic() - started:.1f}s")
    return results
