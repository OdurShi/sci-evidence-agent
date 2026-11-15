"""Record the HTTP fixtures used by the offline tests from the live APIs.

    python scripts/record_fixtures.py            # PubChem, ChEMBL, UniProt, PubMed
    python scripts/record_fixtures.py --lotus    # also Wikidata (about 3 minutes)

Each fixture is one JSON file: the request, the status code, a few response
headers and the body. Bodies are trimmed to the fields the connectors read
(plus a little context) so that the files stay small. Requests are spaced
generously; the Wikidata Query Service gets one request per 65 seconds.

Fixtures for failures that cannot be provoked on demand (HTTP 5xx, 429) are
written by hand in tests/fixtures/synthetic/ and are not touched here.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import httpx

from sci_evidence_agent import config
from sci_evidence_agent.connectors.lotus import USED_PROPERTIES

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
KEPT_HEADERS = (
    "content-type",
    "x-total-results",
    "x-uniprot-release",
    "x-uniprot-release-date",
    "x-throttling-control",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
)


def fetch(url: str, params: dict[str, Any] | None = None, accept: str = "application/json",
          pause: float = 1.0) -> httpx.Response:  # fmt: skip
    time.sleep(pause)
    return httpx.get(
        url,
        params=params,
        headers={"User-Agent": config.user_agent(), "Accept": accept},
        timeout=60.0,
        follow_redirects=True,
    )


def save(group: str, name: str, resp: httpx.Response, body: Any = None,
         params: dict[str, Any] | None = None) -> None:  # fmt: skip
    if body is None:
        is_json = "json" in resp.headers.get("content-type", "")
        body = resp.json() if is_json else resp.text[:300]
    doc = {
        "recorded": time.strftime("%Y-%m-%d"),
        "request": {"url": str(resp.request.url).split("?")[0], "params": params or {}},
        "status": resp.status_code,
        "headers": {k: v for k, v in resp.headers.items() if k.lower() in KEPT_HEADERS},
        "body": body,
    }
    path = OUT / group / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"  {group}/{name}.json  HTTP {resp.status_code}  {path.stat().st_size} bytes")


def pick(obj: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: obj[k] for k in keys if k in obj}


# -- PubChem -------------------------------------------------------------------------


def record_pubchem() -> None:
    base = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound"
    props = "MolecularFormula,SMILES,InChIKey,MolecularWeight,IUPACName"
    for name, path in (
        ("aspirin_by_name", f"name/aspirin/property/{props}/JSON"),
        ("cid_2244", f"cid/2244/property/{props}/JSON"),
        ("name_not_found", f"name/zzzznotacompound/property/{props}/JSON"),
        ("stearate_two_cids", f"name/stearate/property/{props}/JSON"),
        ("cid_without_record", f"cid/999999999/property/{props}/JSON"),
    ):
        save("pubchem", name, fetch(f"{base}/{path}"))


# -- ChEMBL --------------------------------------------------------------------------

MOLECULE_KEYS = ("molecule_chembl_id", "pref_name", "molecule_type", "max_phase")
ACTIVITY_KEYS = (
    "activity_id", "assay_chembl_id", "assay_description", "assay_type", "document_chembl_id",
    "molecule_chembl_id", "molecule_pref_name", "standard_relation", "standard_type",
    "standard_units", "standard_value", "target_chembl_id", "target_organism",
    "target_pref_name",
)  # fmt: skip


def trim_molecule(m: dict[str, Any]) -> dict[str, Any]:
    out = pick(m, MOLECULE_KEYS)
    out["molecule_properties"] = pick(
        m.get("molecule_properties") or {}, ("full_molformula", "full_mwt")
    )
    out["molecule_structures"] = pick(
        m.get("molecule_structures") or {}, ("canonical_smiles", "standard_inchi_key")
    )
    return out


def record_chembl() -> None:
    base = "https://www.ebi.ac.uk/chembl/api/data"
    for name, query, limit in (
        ("molecule_search_aspirin", "aspirin", 3),
        ("molecule_search_paracetamol", "paracetamol", 5),
        ("molecule_search_none", "zzzznotacompound", 5),
    ):
        params = {"q": query, "limit": limit}
        resp = fetch(f"{base}/molecule/search.json", params)
        data = resp.json()
        body = {
            "molecules": [trim_molecule(m) for m in data["molecules"]],
            "page_meta": data["page_meta"],
        }
        save("chembl", name, resp, body, params)

    resp = fetch(f"{base}/molecule/CHEMBL25.json")
    save("chembl", "molecule_CHEMBL25", resp, trim_molecule(resp.json()))
    save("chembl", "molecule_unknown_id", fetch(f"{base}/molecule/CHEMBL99999999.json"))

    for name, chembl_id in (("activity_CHEMBL25", "CHEMBL25"), ("activity_none", "CHEMBL99999999")):
        params = {"molecule_chembl_id": chembl_id, "limit": 3}
        resp = fetch(f"{base}/activity.json", params)
        data = resp.json()
        body = {
            "activities": [pick(a, ACTIVITY_KEYS) for a in data["activities"]],
            "page_meta": data["page_meta"],
        }
        save("chembl", name, resp, body, params)


# -- UniProt -------------------------------------------------------------------------

UNIPROT_FIELDS = "accession,protein_name,gene_names,organism_name,organism_id,length,reviewed"


def trim_entry(e: dict[str, Any]) -> dict[str, Any]:
    out = pick(e, ("entryType", "primaryAccession", "uniProtkbId", "inactiveReason"))
    if "secondaryAccessions" in e:
        out["secondaryAccessions"] = e["secondaryAccessions"][:3]
    if "organism" in e:
        out["organism"] = pick(e["organism"], ("scientificName", "commonName", "taxonId"))
    desc = e.get("proteinDescription", {})
    if "recommendedName" in desc:
        out["proteinDescription"] = {
            "recommendedName": {"fullName": desc["recommendedName"].get("fullName", {})}
        }
    if "genes" in e:
        out["genes"] = [pick(g, ("geneName",)) for g in e["genes"]]
    if "sequence" in e:
        out["sequence"] = pick(e["sequence"], ("length", "molWeight"))
    return out


def record_uniprot() -> None:
    base = "https://rest.uniprot.org/uniprotkb"
    for name, query, size in (
        ("search_egfr_human", "(gene:EGFR) AND organism_id:9606 AND reviewed:true", 5),
        ("search_egfr_all_organisms", "(gene:EGFR) AND reviewed:true", 3),
        ("search_none", "(gene:ZZZNOTAGENE) AND organism_id:9606 AND reviewed:true", 5),
        ("search_bad_field", "(kinase: human) AND organism_id:9606 AND reviewed:true", 5),
        ("search_or_unparenthesised", "gene:EGFR OR gene:ERBB2 AND organism_id:9606", 3),
        (
            "search_or_parenthesised",
            "(gene:EGFR OR gene:ERBB2) AND organism_id:9606 AND reviewed:true",
            3,
        ),
    ):  # fmt: skip
        params = {"query": query, "fields": UNIPROT_FIELDS, "format": "json", "size": size}
        resp = fetch(f"{base}/search", params)
        body = None
        if resp.status_code == 200:
            body = {"results": [trim_entry(e) for e in resp.json()["results"]]}
        save("uniprot", name, resp, body, params)

    for name, accession in (
        ("entry_P00533", "P00533"),
        ("entry_inactive_Q9ZZZ9", "Q9ZZZ9"),
        ("entry_secondary_P06268", "P06268"),
    ):
        resp = fetch(f"{base}/{accession}.json")
        save("uniprot", name, resp, trim_entry(resp.json()))


# -- PubMed --------------------------------------------------------------------------

SUMMARY_KEYS = (
    "uid", "pubdate", "source", "title", "fulljournalname", "articleids", "error",
)  # fmt: skip


def trim_summary(data: dict[str, Any]) -> dict[str, Any]:
    result = data.get("result", {})
    out: dict[str, Any] = {"uids": result.get("uids", [])}
    for uid in out["uids"]:
        entry = pick(result[uid], SUMMARY_KEYS)
        if "authors" in result[uid]:
            entry["authors"] = result[uid]["authors"][:3]
        out[uid] = entry
    trimmed = {"header": data.get("header"), "result": out}
    if "error" in data:
        trimmed["error"] = data["error"]
    return trimmed


def record_pubmed() -> None:
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    ident = {"tool": "sci-evidence-agent", "db": "pubmed", "retmode": "json"}
    for name, term in (
        ("esearch_aspirin_cyclooxygenase", "aspirin AND cyclooxygenase"),
        ("esearch_none", "zzzznotarealquery12345"),
        ("esearch_unknown_field_tag", "aspirin[notafield]"),
        ("esearch_empty_term", ""),
    ):
        params = {**ident, "term": term, "retmax": 3, "sort": "relevance"}
        resp = fetch(f"{base}/esearch.fcgi", params)
        save("pubmed", name, resp, None, params)
        if name == "esearch_aspirin_cyclooxygenase":
            ids = resp.json()["esearchresult"]["idlist"]
            params = {**ident, "id": ",".join(ids)}
            resp = fetch(f"{base}/esummary.fcgi", params)
            save(
                "pubmed", "esummary_aspirin_cyclooxygenase", resp, trim_summary(resp.json()), params
            )

    for name, pmid in (
        ("esummary_14592543", "14592543"),
        ("esummary_invalid_pmid", "999999999"),
        ("esummary_not_a_number", "abc"),
    ):
        params = {**ident, "id": pmid}
        resp = fetch(f"{base}/esummary.fcgi", params)
        save("pubmed", name, resp, trim_summary(resp.json()), params)


# -- LOTUS / Wikidata ----------------------------------------------------------------

SPARQL = "https://query.wikidata.org/sparql"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"


def trim_entity(e: dict[str, Any]) -> dict[str, Any]:
    claims = {
        pid: [{"mainsnak": s.get("mainsnak", {}), "rank": s.get("rank")} for s in e["claims"][pid]]
        for pid in USED_PROPERTIES
        if pid in e.get("claims", {})
    }
    labels = {"en": e["labels"]["en"]} if "en" in e.get("labels", {}) else {}
    return {"type": e.get("type"), "id": e.get("id"), "labels": labels, "claims": claims}


def record_lotus() -> None:
    def sparql(name: str, query: str, pause: float) -> dict[str, Any]:
        params = {"query": query, "format": "json"}
        resp = fetch(SPARQL, params, accept="application/sparql-results+json", pause=pause)
        if resp.status_code != 200:
            raise SystemExit(f"Wikidata answered HTTP {resp.status_code}; try again later")
        save("lotus", name, resp, None, params)
        return resp.json()

    def entities(name: str, qids: list[str]) -> None:
        params = {
            "action": "wbgetentities",
            "ids": "|".join(qids),
            "format": "json",
            "props": "labels|claims",
            "languages": "en",
        }
        resp = fetch(WIKIDATA_API, params, pause=2.0)
        data = resp.json()
        body = {
            "entities": {q: trim_entity(e) for q, e in data.get("entities", {}).items()},
            "success": data.get("success"),
        }
        save("lotus", name, resp, body, params)

    def qid(uri: str) -> str:
        return uri.rsplit("/", 1)[-1]

    # search_by_taxon("Artemisia annua", limit=3) asks for LIMIT 12.
    data = sparql(
        "sparql_taxon_artemisia_annua",
        """
SELECT ?compound ?inchikey ?smiles ?ref ?taxonItem WHERE {
  ?taxonItem wdt:P225 "Artemisia annua" .
  ?compound p:P703 ?stmt .
  ?stmt ps:P703 ?taxonItem .
  OPTIONAL { ?compound wdt:P235 ?inchikey }
  OPTIONAL { ?compound wdt:P233 ?smiles }
  OPTIONAL { ?stmt prov:wasDerivedFrom/pr:P248 ?ref }
} LIMIT 12
""",
        pause=1.0,
    )
    rows = data["results"]["bindings"]
    compounds = list(dict.fromkeys(qid(r["compound"]["value"]) for r in rows))[:3]
    refs = list(
        dict.fromkeys(
            qid(r["ref"]["value"])
            for r in rows
            if "ref" in r and qid(r["compound"]["value"]) in compounds
        )
    )
    entities("entities_taxon_artemisia_annua", compounds + refs)

    # search_occurrences(artemisinin, limit=3) asks for LIMIT 4.
    data = sparql(
        "sparql_occurrences_artemisinin",
        """
SELECT ?compound ?taxonItem ?taxonName WHERE {
  ?compound wdt:P235 "BLUAFEHZUWYNDE-NNWCWBAJSA-N" .
  ?compound p:P703 ?stmt .
  ?stmt ps:P703 ?taxonItem .
  ?taxonItem wdt:P225 ?taxonName .
} LIMIT 4
""",
        pause=65.0,
    )
    rows = data["results"]["bindings"]
    if rows:
        entities("entities_occurrences_artemisinin", [qid(rows[0]["compound"]["value"])])

    sparql(
        "sparql_taxon_none",
        """
SELECT ?compound ?inchikey ?smiles ?ref ?taxonItem WHERE {
  ?taxonItem wdt:P225 "Zzzznotaspecies annua" .
  ?compound p:P703 ?stmt .
  ?stmt ps:P703 ?taxonItem .
  OPTIONAL { ?compound wdt:P235 ?inchikey }
  OPTIONAL { ?compound wdt:P233 ?smiles }
  OPTIONAL { ?stmt prov:wasDerivedFrom/pr:P248 ?ref }
} LIMIT 12
""",
        pause=65.0,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lotus", action="store_true", help="also record Wikidata fixtures")
    parser.add_argument("--only-lotus", action="store_true", help="record only Wikidata fixtures")
    args = parser.parse_args()
    if not args.only_lotus:
        for label, recorder in (
            ("PubChem", record_pubchem),
            ("ChEMBL", record_chembl),
            ("UniProt", record_uniprot),
            ("PubMed", record_pubmed),
        ):
            print(label)
            recorder()
    if args.lotus or args.only_lotus:
        print("LOTUS / Wikidata (one SPARQL query per 65 s)")
        record_lotus()


if __name__ == "__main__":
    main()
