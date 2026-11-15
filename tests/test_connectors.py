"""Connectors against recorded responses: every state each connector can produce.

The four result states are ok / no_match / ambiguous / error. PubMed, LOTUS
and ``get_bioactivities`` have no ambiguous state by design (a search returns
a ranked list, a chain step works on one identifier); the tests at the end of
each section pin that down.
"""

from __future__ import annotations

import json

import httpx
import pytest

from helpers import load_fixture
from sci_evidence_agent.connectors import (
    get_article,
    get_bioactivities,
    search_by_taxon,
    search_chembl_molecule,
    search_compound,
    search_literature,
    search_occurrences,
    search_protein,
)
from sci_evidence_agent.connectors.base import clamp_limit
from sci_evidence_agent.schemas import Status

# == PubChem ==========================================================================


def test_pubchem_ok_by_name(upstream):
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    result = search_compound("aspirin")

    assert result.status is Status.OK
    assert result.source == "PubChem"
    record = result.records[0]
    assert record["source_id"] == "2244"
    assert record["molecular_formula"] == "C9H8O4"
    assert record["inchikey"] == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
    assert record["smiles"] == "CC(=O)OC1=CC=CC=C1C(=O)O"
    assert record["molecular_weight"] == pytest.approx(180.16)  # upstream sends a string
    assert record["source_url"] == "https://pubchem.ncbi.nlm.nih.gov/compound/2244"
    assert result.source_url == record["source_url"]
    assert (result.truncated, result.total_available) == (False, 1)
    assert result.summary() == "PubChem: 1 record"


def test_pubchem_numeric_query_uses_the_cid_endpoint(upstream):
    upstream.add("/compound/cid/2244/", "pubchem/cid_2244")
    result = search_compound(" 2244 ")
    assert result.status is Status.OK
    assert result.records[0]["source_id"] == "2244"
    assert "/compound/cid/2244/property/" in upstream.urls[0]


def test_pubchem_no_match_is_http_404(upstream):
    upstream.add("/compound/name/zzzznotacompound/", "pubchem/name_not_found")
    result = search_compound("zzzznotacompound")
    assert result.status is Status.NO_MATCH
    assert result.records == [] and result.error is None and result.raw_path is None
    assert result.summary() == "PubChem: no matching record"


def test_pubchem_cid_without_compound_record_is_no_match_not_an_empty_hit(upstream):
    # PubChem answers HTTP 200 with a row that holds nothing but the CID.
    upstream.add("/compound/cid/999999999/", "pubchem/cid_without_record")
    result = search_compound("999999999")
    assert result.status is Status.NO_MATCH
    assert result.records == []
    assert "no compound record" in result.warnings[0]


def test_pubchem_ambiguous_when_a_name_has_several_cids(upstream):
    upstream.add("/compound/name/stearate/", "pubchem/stearate_two_cids")
    result = search_compound("stearate")
    assert result.status is Status.AMBIGUOUS
    assert [r["source_id"] for r in result.records] == ["3033836", "5281"]
    assert result.summary() == "PubChem: 2 candidates; needs confirmation"


def test_pubchem_error_on_overload_page(upstream, sleeper):
    upstream.add("/compound/name/aspirin/", "synthetic/pubchem_503_busy")
    result = search_compound("aspirin")
    assert result.status is Status.ERROR
    assert "HTTP 503 after 3 attempts" in result.error and "Server Busy" in result.error
    assert result.records == [] and result.raw_path is None
    assert len(upstream.requests) == 3
    assert result.summary().startswith("PubChem: request failed")


def test_pubchem_error_on_bad_request(upstream):
    fault = {"Fault": {"Code": "PUGREST.BadRequest", "Message": "Invalid ID"}}
    upstream.add("/compound/cid/0/", httpx.Response(400, json=fault))
    result = search_compound("0")
    assert result.status is Status.ERROR and "HTTP 400" in result.error


def test_pubchem_name_is_percent_encoded_in_the_path(upstream):
    upstream.add("pubchem", "pubchem/name_not_found")
    search_compound("acetyl salicylic/acid?x")
    path = upstream.requests[0].url.raw_path.decode()
    assert "/compound/name/acetyl%20salicylic%2Facid%3Fx/property/" in path


def test_pubchem_limit_truncates_and_throttle_header_becomes_a_warning(upstream):
    doc = load_fixture("pubchem/stearate_two_cids")
    doc["headers"]["x-throttling-control"] = "Request Count status: Yellow (60%)"
    upstream.add("/compound/name/stearate/", doc)
    result = search_compound("stearate", limit=1)
    assert result.status is Status.OK  # one record shown ...
    assert (result.truncated, result.total_available) == (True, 2)  # ... of two
    assert any("throttling" in w for w in result.warnings)


def test_clamp_limit_explains_what_it_changed():
    assert clamp_limit("UniProt", 600) == (
        500,
        ["UniProt returns at most 500 records per request; limit lowered from 600"],
    )
    assert clamp_limit("PubMed", 0)[0] == 1
    assert clamp_limit("ChEMBL", 20) == (20, [])


# == ChEMBL ===========================================================================


def test_chembl_search_ok_when_the_preferred_name_matches_exactly(upstream):
    upstream.add("molecule/search.json", "chembl/molecule_search_aspirin")
    result = search_chembl_molecule("aspirin", limit=3)

    assert result.status is Status.OK  # exact pref_name match settles it
    assert result.records[0]["source_id"] == "CHEMBL25"
    assert result.records[0]["name"] == "ASPIRIN"
    assert result.records[0]["inchikey"] == "BSYNRYMUTXBXSQ-UHFFFAOYSA-N"
    assert len(result.records) == 3
    assert result.truncated is True and result.total_available == 52
    assert result.source_url == "https://www.ebi.ac.uk/chembl/compound_report_card/CHEMBL25/"
    assert "q=aspirin" in upstream.urls[0] and "limit=3" in upstream.urls[0]


def test_chembl_search_ambiguous_without_exact_name(upstream):
    upstream.add("molecule/search.json", "chembl/molecule_search_paracetamol")
    result = search_chembl_molecule("paracetamol", limit=5)
    assert result.status is Status.AMBIGUOUS
    assert "CHEMBL112" in [r["source_id"] for r in result.records]
    assert any("pref_name" in w for w in result.warnings)


def test_chembl_search_no_match(upstream):
    upstream.add("molecule/search.json", "chembl/molecule_search_none")
    assert search_chembl_molecule("zzzznotacompound").status is Status.NO_MATCH


def test_chembl_search_error_after_retries(upstream, sleeper):
    upstream.add("molecule/search.json", "synthetic/chembl_500")
    result = search_chembl_molecule("aspirin")
    assert result.status is Status.ERROR and "HTTP 500 after 3 attempts" in result.error
    assert sleeper.delays == [0.8, 1.6]


def test_chembl_id_is_fetched_directly(upstream):
    upstream.add("molecule/CHEMBL25.json", "chembl/molecule_CHEMBL25")
    result = search_chembl_molecule("chembl25")
    assert result.status is Status.OK
    assert result.records[0]["source_id"] == "CHEMBL25"
    assert result.total_available == 1 and result.truncated is False
    assert upstream.count("search.json") == 0


def test_chembl_unknown_id_is_no_match(upstream):
    upstream.add("molecule/CHEMBL99999999.json", "chembl/molecule_unknown_id")  # 404, HTML body
    assert search_chembl_molecule("CHEMBL99999999").status is Status.NO_MATCH


def test_chembl_activities_ok_and_nulls_are_kept(upstream):
    upstream.add("activity.json", "chembl/activity_CHEMBL25")
    result = get_bioactivities("CHEMBL25", limit=3)

    assert result.status is Status.OK
    assert len(result.records) == 3
    first = result.records[0]
    assert first["molecule_chembl_id"] == "CHEMBL25"
    assert first["target_chembl_id"].startswith("CHEMBL")
    assert first["assay_chembl_id"].startswith("CHEMBL")
    assert first["source_url"].endswith(f"/assay_report_card/{first['assay_chembl_id']}/")
    assert isinstance(first["standard_value"], float)  # upstream sends a string
    assert first["standard_units"] is None  # dimensionless: null stays null
    assert any("have no units" in w for w in result.warnings)
    assert result.truncated is True and result.total_available > 3
    assert "molecule_chembl_id=CHEMBL25" in upstream.urls[0]


def test_chembl_activities_no_match(upstream):
    upstream.add("activity.json", "chembl/activity_none")
    result = get_bioactivities("CHEMBL99999999")
    assert result.status is Status.NO_MATCH and result.records == []


def test_chembl_activities_error(upstream):
    upstream.add("activity.json", httpx.ReadTimeout("slow"))
    result = get_bioactivities("CHEMBL25")
    assert result.status is Status.ERROR and "timed out" in result.error


def test_chembl_activities_have_no_ambiguous_state(upstream):
    upstream.add("activity.json", "chembl/activity_CHEMBL25")
    assert get_bioactivities("CHEMBL25", limit=1).status is Status.OK


# == UniProt ==========================================================================


def test_uniprot_search_ok(upstream):
    upstream.add("uniprotkb/search", "uniprot/search_egfr_human")
    result = search_protein("gene:EGFR", organism_id=9606)

    assert result.status is Status.OK
    record = result.records[0]
    assert record["accession"] == "P00533"
    assert record["protein_name"] == "Epidermal growth factor receptor"
    assert record["gene_names"] == ["EGFR"]
    assert (record["organism"], record["taxonomy_id"]) == ("Homo sapiens", 9606)
    assert record["reviewed"] is True and record["sequence_length"] == 1210
    assert record["source_url"] == "https://www.uniprot.org/uniprotkb/P00533"
    assert any(w.startswith("UniProt release ") for w in result.warnings)  # from a header
    assert "query=(gene:EGFR) AND organism_id:9606 AND reviewed:true" in upstream.urls[0]


def test_uniprot_query_is_parenthesised_so_or_cannot_swallow_the_filters(upstream):
    upstream.add("uniprotkb/search", "uniprot/search_or_parenthesised")
    result = search_protein("gene:EGFR OR gene:ERBB2", organism_id=9606, limit=3)
    assert (
        "query=(gene:EGFR OR gene:ERBB2) AND organism_id:9606 AND reviewed:true"
        in (upstream.urls[0])
    )
    assert {r["taxonomy_id"] for r in result.records} == {9606}
    # The recorded answer to the unparenthesised form shows what would go wrong:
    # the organism filter binds to the second term only and other species leak in.
    leaked = load_fixture("uniprot/search_or_unparenthesised")
    assert int(leaked["headers"]["x-total-results"]) > 2
    assert {e["organism"]["taxonId"] for e in leaked["body"]["results"]} != {9606}


def test_uniprot_search_ambiguous_and_total_comes_from_the_header(upstream):
    upstream.add("uniprotkb/search", "uniprot/search_egfr_all_organisms")
    result = search_protein("gene:EGFR", limit=3)

    assert result.status is Status.AMBIGUOUS
    assert len(result.records) == 3
    assert result.total_available == 6  # X-Total-Results, not len(results)
    assert result.truncated is True
    assert any("several organisms" in w for w in result.warnings)
    assert "organism_id:" not in upstream.urls[0]  # no organism filter was sent


def test_uniprot_search_no_match(upstream):
    upstream.add("uniprotkb/search", "uniprot/search_none")
    result = search_protein("gene:ZZZNOTAGENE", organism_id=9606)
    assert result.status is Status.NO_MATCH
    assert any("only Swiss-Prot was searched" in w for w in result.warnings)


def test_uniprot_search_error_on_malformed_query(upstream):
    upstream.add("uniprotkb/search", "uniprot/search_bad_field")  # real HTTP 400
    result = search_protein("kinase: human", organism_id=9606)
    assert result.status is Status.ERROR
    assert "HTTP 400" in result.error and "not a valid search field" in result.error
    assert len(upstream.requests) == 1  # a client error is not retried


def test_uniprot_accession_is_fetched_directly(upstream):
    upstream.add("uniprotkb/P00533.json", "uniprot/entry_P00533")
    result = search_protein("p00533")
    assert result.status is Status.OK
    assert result.records[0]["accession"] == "P00533"
    assert result.total_available == 1
    assert upstream.count("/search") == 0


def test_uniprot_inactive_accession_is_no_match_with_the_reason(upstream):
    upstream.add("uniprotkb/Q9ZZZ9.json", "uniprot/entry_inactive_Q9ZZZ9")  # HTTP 200, Inactive
    result = search_protein("Q9ZZZ9")
    assert result.status is Status.NO_MATCH
    assert result.records == []
    assert any("inactive UniProtKB entry (DELETED)" in w for w in result.warnings)


def test_uniprot_secondary_accession_is_flagged(upstream):
    upstream.add("uniprotkb/P06268.json", "uniprot/entry_secondary_P06268")
    result = search_protein("P06268")
    assert result.status is Status.OK
    assert result.records[0]["accession"] == "P00533"
    assert any("secondary accession" in w and "P00533" in w for w in result.warnings)


def test_uniprot_unknown_accession_404_is_no_match(upstream):
    upstream.add("uniprotkb/P99998.json", httpx.Response(404, json={"messages": ["not found"]}))
    assert search_protein("P99998").status is Status.NO_MATCH


def test_uniprot_limit_above_cap_is_clamped(upstream):
    upstream.add("uniprotkb/search", "uniprot/search_egfr_human")
    result = search_protein("gene:EGFR", organism_id=9606, limit=600)
    assert "size=500" in upstream.urls[0]
    assert any("limit lowered from 600" in w for w in result.warnings)


# == PubMed ===========================================================================


def pubmed_search_routes(upstream):
    upstream.add("esearch.fcgi", "pubmed/esearch_aspirin_cyclooxygenase")
    upstream.add("esummary.fcgi", "pubmed/esummary_aspirin_cyclooxygenase")


def test_pubmed_search_ok_needs_two_requests(upstream):
    pubmed_search_routes(upstream)
    result = search_literature("aspirin AND cyclooxygenase", limit=3)

    assert result.status is Status.OK
    assert len(upstream.requests) == 2  # esearch, then esummary
    first = result.records[0]
    assert first["pmid"] == "14592543"
    assert first["title"] == "The mechanism of action of aspirin."
    assert first["doi"] == "10.1016/s0049-3848(03)00379-7"  # picked out of articleids
    assert first["doi_url"] == "https://doi.org/10.1016/s0049-3848(03)00379-7"
    assert first["source_url"] == "https://pubmed.ncbi.nlm.nih.gov/14592543/"
    assert first["authors"][0] == "Vane JR"
    assert result.total_available > 1000 and result.truncated is True  # count is a string
    assert any(w.startswith("query executed by PubMed:") for w in result.warnings)
    assert result.source_url == "https://pubmed.ncbi.nlm.nih.gov/?term=aspirin+AND+cyclooxygenase"
    assert "id=14592543," in upstream.urls[1]


def test_pubmed_search_no_match_is_http_200_with_count_zero(upstream):
    upstream.add("esearch.fcgi", "pubmed/esearch_none")
    result = search_literature("zzzznotarealquery12345")
    assert result.status is Status.NO_MATCH
    assert len(upstream.requests) == 1  # no esummary without PMIDs


def test_pubmed_search_error_on_empty_expression(upstream):
    upstream.add("esearch.fcgi", "pubmed/esearch_empty_term")  # HTTP 200 with an ERROR string
    result = search_literature("")
    assert result.status is Status.ERROR
    assert "PubMed rejected the query" in result.error


def test_pubmed_search_error_when_summaries_are_missing(upstream):
    upstream.add("esearch.fcgi", "pubmed/esearch_aspirin_cyclooxygenase")
    upstream.add("esummary.fcgi", httpx.Response(200, json={"result": {"uids": []}}))
    result = search_literature("aspirin AND cyclooxygenase", limit=3)
    assert result.status is Status.ERROR
    assert "esummary returned none" in result.error


def test_pubmed_search_error_on_rate_limit_then_recovers(upstream, sleeper):
    upstream.add("esearch.fcgi", "synthetic/pubmed_429", "pubmed/esearch_aspirin_cyclooxygenase")
    upstream.add("esummary.fcgi", "pubmed/esummary_aspirin_cyclooxygenase")
    result = search_literature("aspirin AND cyclooxygenase", limit=3)
    assert result.status is Status.OK
    assert sleeper.delays == [2.0]  # the Retry-After of the 429


def test_pubmed_unknown_field_tag_is_reported_not_hidden(upstream):
    # PubMed drops "[notafield]" silently and searches for plain "aspirin".
    upstream.add("esearch.fcgi", "pubmed/esearch_unknown_field_tag")
    upstream.add("esummary.fcgi", "pubmed/esummary_aspirin_cyclooxygenase")
    result = search_literature("aspirin[notafield]", limit=3)
    assert any("unrecognised field tag is dropped silently" in w for w in result.warnings)
    assert any(w.startswith("query executed by PubMed:") for w in result.warnings)


def test_pubmed_invalid_field_reported_by_upstream_is_an_error(upstream):
    body = {"esearchresult": {"count": "0", "idlist": [], "errorlist": {"fieldsnotfound": ["zz"]}}}
    upstream.add("esearch.fcgi", httpx.Response(200, json=body))
    result = search_literature("aspirin[zz]")
    assert result.status is Status.ERROR and "invalid field" in result.error


def test_pubmed_get_article_ok(upstream):
    upstream.add("esummary.fcgi", "pubmed/esummary_14592543")
    result = get_article(" 14592543 ")
    assert result.status is Status.OK
    assert result.records[0]["pmid"] == "14592543"
    assert result.records[0]["journal"] == "Thrombosis research"
    assert result.records[0]["doi"] == "10.1016/s0049-3848(03)00379-7"
    assert result.warnings == []


def test_pubmed_invalid_pmid_is_no_match_although_http_is_200(upstream):
    upstream.add("esummary.fcgi", "pubmed/esummary_invalid_pmid")
    result = get_article("999999999")
    assert result.status is Status.NO_MATCH
    assert result.warnings == ["PubMed: cannot get document summary"]


def test_pubmed_non_numeric_pmid_is_no_match_with_upstream_reason(upstream):
    upstream.add("esummary.fcgi", "pubmed/esummary_not_a_number")
    result = get_article("abc")
    assert result.status is Status.NO_MATCH
    assert result.warnings and "Invalid uid" in result.warnings[0]


def test_pubmed_get_article_error(upstream):
    upstream.add("esummary.fcgi", httpx.Response(502, text="bad gateway"))
    assert get_article("14592543").status is Status.ERROR


def test_pubmed_has_no_ambiguous_state(upstream):
    pubmed_search_routes(upstream)
    assert search_literature("aspirin AND cyclooxygenase", limit=3).status is Status.OK


def test_pubmed_credentials_are_sent_but_never_stored(upstream, store, monkeypatch):
    monkeypatch.setenv("NCBI_API_KEY", "SECRETKEY123")
    monkeypatch.setenv("NCBI_EMAIL", "someone@example.org")
    pubmed_search_routes(upstream)
    upstream.add("pubmed", "pubmed/esearch_none")

    ok = search_literature("aspirin AND cyclooxygenase", limit=3)
    upstream._routes.insert(0, (lambda url: "esearch" in url, ["pubmed/esearch_none"]))
    none = search_literature("zzzznotarealquery12345")

    # Sent upstream, as NCBI asks ...
    assert "api_key=SECRETKEY123" in upstream.urls[0]
    assert "email=someone@example.org" in upstream.urls[0]
    # ... but absent from everything that is kept.
    kept = ok.model_dump_json() + none.model_dump_json()
    kept += store.resolve_raw(ok.raw_path).read_text(encoding="utf-8")
    kept += store.db_path.read_bytes().decode("utf-8", errors="ignore")
    assert "SECRETKEY123" not in kept
    assert "someone@example.org" not in kept and "someone%40example.org" not in kept
    assert none.source_url.startswith("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch")
    assert "tool=sci-evidence-agent" in none.source_url


# == LOTUS (Wikidata) =================================================================


def lotus_taxon_routes(upstream):
    upstream.add("query.wikidata.org/sparql", "lotus/sparql_taxon_artemisia_annua")
    upstream.add("wbgetentities", "lotus/entities_taxon_artemisia_annua")


def test_lotus_by_taxon_ok_with_references(upstream, store):
    lotus_taxon_routes(upstream)
    result = search_by_taxon("Artemisia annua", limit=3)

    assert result.status is Status.OK
    assert len(result.records) == 3
    assert len(upstream.requests) == 2  # one SPARQL query, one wbgetentities batch
    for record in result.records:
        assert record["wikidata_id"].startswith("Q")
        assert record["taxon_name"] == "Artemisia annua"
        assert record["taxon_wikidata_id"].startswith("Q")
        assert record["source_url"] == f"https://www.wikidata.org/wiki/{record['wikidata_id']}"
    assert any(r["inchikey"] for r in result.records)
    cited = [r for r in result.records if r["references"]]
    assert cited, "at least one pair must carry a literature reference"
    reference = cited[0]["references"][0]
    assert reference["doi"] and reference["url"].startswith("https://doi.org/")
    assert reference["year"] and reference["year"].isdigit()
    assert cited[0]["reference_count"] == len(cited[0]["references"])
    # 12 rows were requested (limit * 4) and 12 came back: the total is unknown.
    assert result.truncated is True and result.total_available is None
    assert "LIMIT 12" in upstream.urls[0]
    assert "?stmt ps:P703 ?taxonItem" in upstream.urls[0]  # binds compound and taxon

    # The archive holds the real responses, not just counts.
    raw = store.read_raw(result.raw_path)["response"]
    assert len(raw["sparql"]["results"]["bindings"]) == 12
    assert set(raw["entities"]) >= {r["wikidata_id"] for r in result.records}
    assert reference["doi"] in json.dumps(raw["entities"])


def test_lotus_by_taxon_no_match(upstream):
    upstream.add("query.wikidata.org/sparql", "lotus/sparql_taxon_none")
    result = search_by_taxon("Zzzznotaspecies annua", limit=3)
    assert result.status is Status.NO_MATCH
    assert any("accepted scientific name" in w for w in result.warnings)
    assert len(upstream.requests) == 1  # no entity lookup without rows


def test_lotus_error_on_usage_block_is_reported_at_once(upstream, sleeper):
    upstream.add("query.wikidata.org/sparql", "synthetic/wikidata_429_blocked")
    result = search_by_taxon("Artemisia annua", limit=3)
    assert result.status is Status.ERROR
    assert "HTTP 429" in result.error and "about 17 min" in result.error
    assert "not suited to frequent interactive queries" in result.error
    assert "192.0.2.44" not in result.error  # the error page echoes the client address
    assert len(upstream.requests) == 1 and sleeper.delays == []


def test_lotus_error_on_gateway_timeout_is_not_retried(upstream, sleeper):
    upstream.add("query.wikidata.org/sparql", "synthetic/wikidata_504")
    result = search_by_taxon("Artemisia annua", limit=3)
    assert result.status is Status.ERROR
    assert "HTTP 504" in result.error and "Lower the limit" in result.error
    assert len(upstream.requests) == 1 and sleeper.delays == []


def test_lotus_short_rate_limit_pause_is_retried(upstream, sleeper):
    upstream.add(
        "query.wikidata.org/sparql",
        httpx.Response(429, headers={"Retry-After": "5"}),
        "lotus/sparql_taxon_none",
    )
    assert search_by_taxon("Zzzznotaspecies annua", limit=3).status is Status.NO_MATCH
    assert sleeper.delays == [5.0]


def test_lotus_taxon_is_escaped_in_the_sparql_literal(upstream):
    upstream.add("query.wikidata.org/sparql", "lotus/sparql_taxon_none")
    search_by_taxon('Evil" . ?x ?y ?z . #', limit=3)
    assert 'wdt:P225 "Evil\\" . ?x ?y ?z . #" .' in upstream.urls[0]


def test_lotus_occurrences_ok_and_truncation_is_detected(upstream, store):
    upstream.add("query.wikidata.org/sparql", "lotus/sparql_occurrences_artemisinin")
    upstream.add("wbgetentities", "lotus/entities_occurrences_artemisinin")
    result = search_occurrences("bluafehzuwynde-nnwcwbajsa-n", limit=3)

    assert result.status is Status.OK
    assert len(result.records) == 3
    assert all(r["inchikey"] == "BLUAFEHZUWYNDE-NNWCWBAJSA-N" for r in result.records)
    assert all(r["taxon_name"] and r["taxon_wikidata_id"].startswith("Q") for r in result.records)
    assert result.records[0]["compound_name"] == "artemisinin"
    assert "LIMIT 4" in upstream.urls[0]  # one more than asked, to detect truncation
    assert result.truncated is True and result.total_available is None
    raw = store.read_raw(result.raw_path)["response"]
    assert "BLUAFEHZUWYNDE-NNWCWBAJSA-N" in json.dumps(raw["entities"])


def test_lotus_occurrences_no_match(upstream):
    empty = {"head": {"vars": []}, "results": {"bindings": []}}
    upstream.add("query.wikidata.org/sparql", httpx.Response(200, json=empty))
    result = search_occurrences("AAAAAAAAAAAAAA-AAAAAAAAAA-A")
    assert result.status is Status.NO_MATCH


def test_lotus_malformed_inchikey_is_an_error_without_a_request(upstream):
    result = search_occurrences("not-an-inchikey")
    assert result.status is Status.ERROR
    assert "not a full InChIKey" in result.error
    assert upstream.requests == []


def test_lotus_unexpected_response_shape_is_an_error(upstream):
    upstream.add("query.wikidata.org/sparql", httpx.Response(200, json={"unexpected": True}))
    result = search_by_taxon("Artemisia annua")
    assert result.status is Status.ERROR and "unexpected SPARQL response shape" in result.error


def test_lotus_has_no_ambiguous_state(upstream):
    lotus_taxon_routes(upstream)
    assert search_by_taxon("Artemisia annua", limit=3).status is Status.OK
