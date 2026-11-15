"""Citation verifier: identifier extraction and traceability to raw responses."""

from __future__ import annotations

import pytest

from helpers import load_fixture
from sci_evidence_agent.agent import ask
from sci_evidence_agent.citations import (
    ECHOED,
    TRACEABLE,
    UNTRACEABLE,
    Identifier,
    RawDocument,
    extract_identifiers,
    identifiers_in_raw,
    possible_pmids,
    verify_answer,
    verify_run,
)
from sci_evidence_agent.identifiers import clean_doi


def ids(text: str, kind: str | None = None) -> set[str]:
    return {i.value for i in extract_identifiers(text) if kind is None or i.kind == kind}


# -- extraction from free text ---------------------------------------------------------


def test_chembl_ids():
    assert ids("CHEMBL25, chembl941 and (CHEMBL3253).", "chembl") == {
        "CHEMBL25",
        "CHEMBL941",
        "CHEMBL3253",
    }
    assert ids("XCHEMBL25 CHEMBL25X", "chembl") == set()


def test_inchikeys():
    text = "InChIKey: BSYNRYMUTXBXSQ-UHFFFAOYSA-N; `BLUAFEHZUWYNDE-NNWCWBAJSA-N`."
    assert ids(text, "inchikey") == {"BSYNRYMUTXBXSQ-UHFFFAOYSA-N", "BLUAFEHZUWYNDE-NNWCWBAJSA-N"}
    assert ids("BSYNRYMUTXBXSQ-UHFFFAOYSA", "inchikey") == set()  # truncated key


def test_uniprot_accessions_both_formats():
    assert ids("P00533, Q9Y6K9 and A0A024R161 (not P0053 or p00533)", "uniprot") == {
        "P00533",
        "Q9Y6K9",
        "A0A024R161",
    }


def test_molecular_formula_is_not_reported_as_an_accession():
    assert ids("aspirin is C9H8O4, caffeine C8H10N4O2", "uniprot") == set()
    # ... unless the text itself says it is an accession.
    assert ids("UniProt accession K7N5P2", "uniprot") == {"K7N5P2"}


def test_wikidata_qid_is_not_reported_as_an_accession():
    text = "Wikidata Q18216 · [source](https://www.wikidata.org/wiki/Q18216) but UniProt Q13131"
    assert ids(text, "uniprot") == {"Q13131"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("DOI 10.1016/s0049-3848(03)00379-7.", "10.1016/s0049-3848(03)00379-7"),
        ("(doi:10.1038/s41586-021-03819-2)", "10.1038/s41586-021-03819-2"),
        ("https://doi.org/10.1016/J.CELL.2011.02.013,", "10.1016/j.cell.2011.02.013"),
        ("[link](https://doi.org/10.1002/cbdv.201400340)", "10.1002/cbdv.201400340"),
        ("**10.1007/S11418-007-0175-2**", "10.1007/s11418-007-0175-2"),
        ('"doi": "10.1038/newbio231232a0"', "10.1038/newbio231232a0"),
    ],
)
def test_doi_extraction_and_normalisation(text, expected):
    assert ids(text, "doi") == {expected}


def test_known_limitation_sici_dois_are_cut_at_the_angle_bracket():
    # Old SICI-style DOIs contain "<" and ">", which also delimit autolinks. The
    # extractor stops there. Answer and raw response are cut the same way, so
    # such a DOI is still matched, but only on its prefix.
    text = "see 10.1002/(SICI)1097-4601(1996)28:1<1::AID-KIN1>3.0.CO;2-Z"
    assert ids(text, "doi") == {"10.1002/(sici)1097-4601(1996)28:1"}


def test_doi_cleaning_keeps_balanced_brackets():
    assert clean_doi("10.1016/S0031-9422(00)83762-0)") == "10.1016/s0031-9422(00)83762-0"
    assert clean_doi("10.1000/abc].") == "10.1000/abc"
    assert clean_doi("10.1000/a[b]") == "10.1000/a[b]"


def test_identifier_like_text_inside_a_doi_is_not_extracted_separately():
    found = extract_identifiers("DOI 10.1234/CHEMBL25.P00533")
    assert found == {Identifier("doi", "10.1234/chembl25.p00533")}


@pytest.mark.parametrize(
    "text",
    [
        "CID 2244",
        "CID: 2244.",
        "PubChem CID **2244**",
        "`CID 2244`",
        "(CID 2244)",
        "cid=2244",
        "https://pubchem.ncbi.nlm.nih.gov/compound/2244",
        '{"cid": "2244"}',
    ],
)
def test_cid_needs_a_label_or_a_pubchem_url(text):
    assert ids(text, "cid") == {"2244"}


@pytest.mark.parametrize(
    "text",
    [
        "PMID 14592543",
        "PMID: 14592543,",
        "PMID:[14592543](https://example.org)",
        "PubMed ID 14592543",
        "https://pubmed.ncbi.nlm.nih.gov/14592543/",
        "https://www.ncbi.nlm.nih.gov/pubmed/14592543",
    ],
)
def test_pmid_needs_a_label_or_a_pubmed_url(text):
    assert ids(text, "pmid") == {"14592543"}


def test_numbers_without_a_label_are_not_identifiers():
    text = "MW 180.16, 4087 records, year 2003, value 100000 nM, acid 5, placid 7"
    assert extract_identifiers(text) == set()


def test_lists_need_a_plural_label_and_decimals_are_never_swallowed():
    assert ids("PMIDs 14592543, 34265844 and 5284360.", "pmid") == {
        "14592543",
        "34265844",
        "5284360",
    }
    assert ids("CIDs: 2244; 5281", "cid") == {"2244", "5281"}
    assert ids("CID 2244, 180.16 g/mol, 5 records", "cid") == {"2244"}
    assert ids("PMID 14592543 and PMID 5284360", "pmid") == {"14592543", "5284360"}


def test_markdown_table_columns():
    table = (
        "| PMID | Title | PubChem CID |\n"
        "|---|---|---|\n"
        "| 14592543 | The mechanism of action of aspirin. | 2244 |\n"
        "| **34265844** | AlphaFold | n/a |\n"
        "\nUnrelated 7654321 afterwards.\n"
    )
    found = extract_identifiers(table)
    assert {i.value for i in found if i.kind == "pmid"} == {"14592543", "34265844"}
    assert {i.value for i in found if i.kind == "cid"} == {"2244"}
    assert possible_pmids(table) == {"7654321"}


def test_possible_pmids_are_bare_seven_or_eight_digit_numbers():
    text = "See 14592543 and 123456 and 123456789; PMID 34265844; CHEMBL1234567; 10.1000/7654321"
    assert possible_pmids(text) == {"14592543"}


# -- raw responses -----------------------------------------------------------------------


def test_raw_identifiers_are_typed_by_source():
    pubchem = load_fixture("pubchem/aspirin_by_name")["body"]
    found = identifiers_in_raw(pubchem, "PubChem")
    assert Identifier("cid", "2244") in found
    assert Identifier("inchikey", "BSYNRYMUTXBXSQ-UHFFFAOYSA-N") in found
    assert not any(i.kind == "pmid" for i in found)
    assert not any(i.kind == "uniprot" for i in found)  # C9H8O4 is a formula

    pubmed = load_fixture("pubmed/esummary_14592543")["body"]
    found = identifiers_in_raw(pubmed, "PubMed")
    assert Identifier("pmid", "14592543") in found
    assert Identifier("doi", "10.1016/s0049-3848(03)00379-7") in found
    assert not any(i.kind == "cid" for i in found)


def test_a_number_in_the_wrong_source_does_not_vouch_for_a_pmid():
    # ChEMBL activity IDs and years are integers too; they must not make a PMID traceable.
    chembl = {"activities": [{"activity_id": 14592543, "document_year": 2003}]}
    report = verify_answer("See PMID 14592543.", [RawDocument("ChEMBL", chembl)])
    assert report.findings == {Identifier("pmid", "14592543"): UNTRACEABLE}
    pubmed = {"result": {"uids": ["14592543"]}}
    report = verify_answer("See PMID 14592543.", [RawDocument("PubMed", pubmed)])
    assert report.findings == {Identifier("pmid", "14592543"): TRACEABLE}


def test_only_whole_integer_values_count_as_numbers():
    raw = {"PropertyTable": {"Properties": [{"CID": 2244, "MolecularWeight": "180.16"}]}}
    found = identifiers_in_raw(raw, "PubChem")
    assert {i.value for i in found if i.kind == "cid"} == {"2244"}  # not 180 or 16


def test_lotus_raw_vouches_for_dois_pmids_and_inchikeys():
    raw = {
        "sparql": load_fixture("lotus/sparql_taxon_artemisia_annua")["body"],
        "entities": load_fixture("lotus/entities_taxon_artemisia_annua")["body"]["entities"],
    }
    found = identifiers_in_raw(raw, "LOTUS")
    kinds = {i.kind for i in found}
    assert {"doi", "inchikey"} <= kinds
    assert all(i.value == i.value.lower() for i in found if i.kind == "doi")


# -- verification ------------------------------------------------------------------------

RAW = [
    RawDocument("PubChem", load_fixture("pubchem/aspirin_by_name")["body"]),
    RawDocument("PubMed", load_fixture("pubmed/esummary_14592543")["body"]),
    RawDocument("UniProt", load_fixture("uniprot/entry_P00533")["body"]),
    RawDocument("ChEMBL", load_fixture("chembl/activity_CHEMBL25")["body"]),
]
GROUNDED = (
    "Aspirin is PubChem CID 2244 (InChIKey BSYNRYMUTXBXSQ-UHFFFAOYSA-N, C9H8O4). "
    "Its mechanism is reviewed in PMID 14592543, DOI 10.1016/S0049-3848(03)00379-7. "
    "ChEMBL lists CHEMBL25; EGFR is UniProt P00533."
)


def test_grounded_answer_is_fully_traceable():
    report = verify_answer(GROUNDED, RAW)
    assert report.ok
    assert report.counts() == {"identifiers": 6, TRACEABLE: 6, ECHOED: 0, UNTRACEABLE: 0}
    assert report.by_kind()["doi"] == {TRACEABLE: 1, ECHOED: 0, UNTRACEABLE: 0}


@pytest.mark.parametrize(
    ("kind", "sentence", "value"),
    [
        ("cid", "It is also listed as CID 99999999.", "99999999"),
        ("chembl", "See CHEMBL4999999 for a close analogue.", "CHEMBL4999999"),
        ("uniprot", "The target is UniProt P12345.", "P12345"),
        ("pmid", "As shown by Smith et al. (PMID 31234567).", "31234567"),
        ("doi", "Smith et al., doi:10.1016/j.cell.2019.01.001.", "10.1016/j.cell.2019.01.001"),
        ("inchikey", "InChIKey RZVAJINKPMORJF-UHFFFAOYSA-N.", "RZVAJINKPMORJF-UHFFFAOYSA-N"),
    ],
)
def test_fabricated_identifier_is_detected(kind, sentence, value):
    report = verify_answer(GROUNDED + " " + sentence, RAW)
    assert not report.ok
    assert report.untraceable == [Identifier(kind, value)]
    assert report.counts()[TRACEABLE] == 6  # the genuine ones are unaffected


def test_fabricated_pmid_with_a_plausible_shape_is_detected_even_with_real_neighbours():
    answer = "Key papers: PMIDs 14592543 and 14592544."  # the second one is off by one
    report = verify_answer(answer, RAW)
    assert report.findings[Identifier("pmid", "14592543")] == TRACEABLE
    assert report.untraceable == [Identifier("pmid", "14592544")]


def test_identifier_supplied_by_the_user_is_echoed_not_fabricated():
    answer = "PMID 999999999: no matching record."
    report = verify_answer(answer, [], echo_text="Retrieve the article with PMID 999999999.")
    assert report.ok
    assert report.findings == {Identifier("pmid", "999999999"): ECHOED}
    # Without the question the same identifier is untraceable.
    assert not verify_answer(answer, []).ok


def test_doi_comparison_is_case_insensitive():
    raw = [RawDocument("LOTUS", {"claims": {"P356": "10.1007/S11418-007-0175-2"}})]
    assert verify_answer("doi 10.1007/s11418-007-0175-2", raw).ok


def test_strict_mode_fails_on_unlabelled_numbers():
    answer = "Relevant papers: 31234567 and 14592543."
    lenient = verify_answer(answer, RAW)
    assert lenient.ok and lenient.possible_pmids_untraced == ["31234567"]
    assert not verify_answer(answer, RAW, strict=True).ok


def test_report_serialises():
    data = verify_answer(GROUNDED + " PMID 31234567.", RAW).to_dict()
    assert data["ok"] is False
    assert data["untraceable_identifiers"] == ["pmid:31234567"]
    assert data["identifiers"] == 7 and data["traceable"] == 6


# -- end to end through the pipeline ---------------------------------------------------


def run_aspirin(upstream):
    upstream.add("/compound/name/aspirin/", "pubchem/aspirin_by_name")
    upstream.add("esearch.fcgi", "pubmed/esearch_aspirin_cyclooxygenase")
    upstream.add("esummary.fcgi", "pubmed/esummary_aspirin_cyclooxygenase")
    return ask("aspirin 的化学信息和相关文献", provider="offline")


def test_offline_answer_is_traceable_to_the_archive(upstream, store):
    run = run_aspirin(upstream)
    assert run.tool_rounds() == [["search_compound", "search_literature"]]
    report = verify_run(run, store)
    assert report.ok and report.missing_raw == []
    assert report.total >= 8  # CID, InChIKey, three PMIDs and their DOIs
    assert report.counts()[UNTRACEABLE] == 0


def test_tampered_answer_is_caught(upstream, store):
    run = run_aspirin(upstream)
    run.answer += "\nAlso relevant: PMID 31234567 and CHEMBL4999999."
    report = verify_run(run, store)
    assert not report.ok
    assert set(report.untraceable) == {
        Identifier("pmid", "31234567"),
        Identifier("chembl", "CHEMBL4999999"),
    }


def test_missing_raw_file_is_reported_and_its_identifiers_become_untraceable(upstream, store):
    run = run_aspirin(upstream)
    store.resolve_raw(run.results[0].raw_path).unlink()
    report = verify_run(run, store)
    assert report.missing_raw == [run.results[0].raw_path]
    assert Identifier("cid", "2244") in report.untraceable
