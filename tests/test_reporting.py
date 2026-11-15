"""Template reporter: wording of the four states and record formatting."""

from __future__ import annotations

from sci_evidence_agent.planning import plan_question
from sci_evidence_agent.reporting import (
    DISCLAIMER,
    NO_ENTITY,
    STATE_TEXT,
    format_record,
    render_report,
)


def payload(status: str, **extra) -> dict:
    return {"status": status, "source": "PubChem", "summary": f"PubChem: {status}", **extra}


def test_report_always_says_it_is_not_model_output():
    assert render_report(None, []).startswith(DISCLAIMER)
    assert "not model output" in DISCLAIMER


def test_no_entity_is_explained():
    plan = plan_question("阿司匹林的分子式是什么？")
    assert NO_ENTITY in render_report(plan, [])


def test_the_four_states_are_worded_differently():
    texts = {
        status: render_report(None, [("search_compound", payload(status, error="HTTP 503"))])
        for status in ("ok", "no_match", "ambiguous", "error")
    }
    assert STATE_TEXT["no_match"] in texts["no_match"]
    assert STATE_TEXT["error"] in texts["error"]
    assert "· error: request failed\n" in texts["error"]
    assert texts["error"].count("HTTP 503") == 1  # the detail is stated once
    assert STATE_TEXT["ambiguous"] in texts["ambiguous"]
    for status, text in texts.items():
        for other, sentence in STATE_TEXT.items():
            if other != status:
                assert sentence not in text
    # A failed request must not read like an empty result, and vice versa.
    assert "not an absence of data" in STATE_TEXT["error"]
    assert "not a failure" in STATE_TEXT["no_match"]


def test_ambiguous_candidates_are_listed():
    records = [
        {"source_db": "PubChem", "source_id": "3033836", "molecular_formula": "C18H35O2-"},
        {"source_db": "PubChem", "source_id": "5281", "molecular_formula": "C18H36O2"},
    ]
    text = render_report(None, [("search_compound", payload("ambiguous", records=records))])
    assert "`CID 3033836`" in text and "`CID 5281`" in text


def test_record_formats_carry_the_identifiers():
    compound = format_record(
        {
            "source_db": "PubChem",
            "source_id": "2244",
            "name": "2-acetyloxybenzoic acid",
            "molecular_formula": "C9H8O4",
            "molecular_weight": 180.16,
            "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
            "smiles": "CC(=O)OC1=CC=CC=C1C(=O)O",
            "source_url": "https://pubchem.ncbi.nlm.nih.gov/compound/2244",
        }
    )
    assert compound.startswith("`CID 2244` · 2-acetyloxybenzoic acid · C9H8O4 · MW 180.16")
    assert "InChIKey BSYNRYMUTXBXSQ-UHFFFAOYSA-N" in compound
    assert "[source](https://pubchem.ncbi.nlm.nih.gov/compound/2244)" in compound

    chembl = format_record({"source_db": "ChEMBL", "source_id": "CHEMBL25", "name": "ASPIRIN"})
    assert chembl == "`CHEMBL25` · ASPIRIN"

    protein = format_record(
        {
            "accession": "P00533",
            "protein_name": "Epidermal growth factor receptor",
            "gene_names": ["EGFR"],
            "organism": "Homo sapiens",
            "taxonomy_id": 9606,
            "reviewed": True,
            "sequence_length": 1210,
        }
    )
    assert protein == (
        "`P00533` · Epidermal growth factor receptor · genes: EGFR · "
        "Homo sapiens (taxonomy ID 9606) · 1210 aa · Swiss-Prot (reviewed)"
    )

    article = format_record(
        {
            "pmid": "14592543",
            "title": "The mechanism of action of aspirin.",
            "authors": ["Vane JR", "Botting RM"],
            "journal": "Thrombosis research",
            "pub_date": "2003 Jun 15",
            "doi": "10.1016/s0049-3848(03)00379-7",
        }
    )
    assert article.startswith(
        "PMID 14592543 · The mechanism of action of aspirin. · Vane JR et al."
    )
    assert article.endswith("DOI 10.1016/s0049-3848(03)00379-7")
    assert "no DOI" in format_record({"pmid": "1", "title": "t"})


def test_missing_measurement_parts_are_stated_not_invented():
    line = format_record(
        {
            "molecule_chembl_id": "CHEMBL25",
            "target_chembl_id": "CHEMBL3253",
            "target_name": "Albumin",
            "assay_chembl_id": "CHEMBL702885",
            "assay_type": "B",
            "standard_type": "Log K'",
            "standard_relation": "=",
            "standard_value": 1.39,
        }
    )
    assert "target CHEMBL3253 (Albumin)" in line and "assay CHEMBL702885" in line
    assert "Log K' = 1.39 (no units)" in line
    assert "(value missing)" in format_record({"activity_id": "1", "standard_type": "IC50"})


def test_occurrence_record_lists_references():
    line = format_record(
        {
            "wikidata_id": "Q255564",
            "compound_name": "trans-anethol",
            "inchikey": "RUVINXPYWBROJD-ONEGZZNKSA-N",
            "taxon_name": "Artemisia annua",
            "taxon_wikidata_id": "Q1308044",
            "references": [
                {"doi": "10.1007/S11418-007-0175-2", "pmid": None, "wikidata_id": "Q104910881"},
                {"doi": None, "pmid": "26460560", "wikidata_id": "Q53813788"},
                {"doi": None, "pmid": None, "wikidata_id": "Q1"},
            ],
        }
    )
    assert line.startswith("trans-anethol · Wikidata Q255564 · InChIKey RUVINXPYWBROJD")
    assert "taxon: Artemisia annua (Wikidata Q1308044)" in line
    assert "references: DOI 10.1007/S11418-007-0175-2; PMID 26460560 (+1 more)" in line


def test_truncation_warnings_and_skips_are_reported():
    records = [{"source_db": "PubChem", "source_id": str(i)} for i in range(12)]
    text = render_report(
        None,
        [
            (
                "search_compound",
                payload(
                    "ok",
                    records=records,
                    truncated=True,
                    total_available=40,
                    warnings=["some records have no IUPAC name"],
                    source_url="https://example.org/x",
                ),
            ),
            ("search_by_taxon", payload("ok", records=records[:1], truncated=True)),
        ],
        skipped=["`get_bioactivities` was not called because ..."],
    )
    assert "... 2 more returned records are not listed here" in text
    assert "Truncated: 12 of 40 records available upstream." in text
    assert "Truncated: 1 records returned; the upstream total is unknown." in text
    assert "Note: some records have no IUPAC name" in text
    assert "Check in the database: https://example.org/x" in text
    assert "Skipped: `get_bioactivities` was not called because ..." in text
