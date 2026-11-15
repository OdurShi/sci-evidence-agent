"""Live tests against the public APIs. Deselected by default.

    pytest -m "live and not lotus"   # PubChem, ChEMBL, UniProt, PubMed (about 30 requests)
    pytest -m lotus                  # Wikidata Query Service; takes about 2 minutes

These mirror the prototype's ``scripts/test_connectors.py`` and
``scripts/test_lotus.py``. They use the package's own rate limiting; the
LOTUS test issues three SPARQL queries, which the limiter spaces by 61 s
because the query service allowed one request per minute when this was written.
"""

from __future__ import annotations

import pytest

from sci_evidence_agent.agent import ask
from sci_evidence_agent.citations import verify_run
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
from sci_evidence_agent.schemas import Status

pytestmark = pytest.mark.live


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda: search_compound("aspirin"), Status.OK),
        (lambda: search_compound("2244"), Status.OK),
        (lambda: search_compound("zzzznotacompound"), Status.NO_MATCH),
        (lambda: search_compound("999999999"), Status.NO_MATCH),
        (lambda: search_compound("stearate"), Status.AMBIGUOUS),
        (lambda: search_chembl_molecule("aspirin"), Status.OK),
        (lambda: search_chembl_molecule("CHEMBL25"), Status.OK),
        (lambda: search_chembl_molecule("paracetamol"), Status.AMBIGUOUS),
        (lambda: get_bioactivities("CHEMBL25", limit=5), Status.OK),
        (lambda: get_bioactivities("CHEMBL99999999", limit=5), Status.NO_MATCH),
        (lambda: search_protein("P00533"), Status.OK),
        (lambda: search_protein("gene:EGFR", organism_id=9606), Status.OK),
        (lambda: search_protein("gene:PTGS1", organism_id=9606), Status.OK),
        (lambda: search_protein("gene:EGFR"), Status.AMBIGUOUS),
        (lambda: search_protein("gene:ZZZNOTAGENE", organism_id=9606), Status.NO_MATCH),
        (lambda: search_protein("Q9ZZZ9"), Status.NO_MATCH),
        (lambda: search_protein("kinase: human", organism_id=9606), Status.ERROR),
        (lambda: search_literature("aspirin AND cyclooxygenase", limit=3), Status.OK),
        (lambda: get_article("14592543"), Status.OK),
        (lambda: search_literature("zzzznotarealquery12345", limit=3), Status.NO_MATCH),
        (lambda: get_article("999999999"), Status.NO_MATCH),
    ],
    ids=[
        "pubchem-name", "pubchem-cid", "pubchem-none", "pubchem-dead-cid", "pubchem-ambiguous",
        "chembl-name", "chembl-id", "chembl-ambiguous", "chembl-activities",
        "chembl-activities-none", "uniprot-accession", "uniprot-egfr", "uniprot-ptgs1",
        "uniprot-ambiguous", "uniprot-none", "uniprot-inactive", "uniprot-error",
        "pubmed-search", "pubmed-article", "pubmed-none", "pubmed-invalid-pmid",
    ],
)  # fmt: skip
def test_connector_state(store, call, expected):
    result = call()
    assert result.status is expected, result.error
    if expected in (Status.OK, Status.AMBIGUOUS):
        assert result.records and result.raw_path


def test_second_call_is_served_from_the_cache(store):
    first = search_compound("aspirin")
    again = search_compound("aspirin")
    assert again.from_cache is True and again.retrieved_at == first.retrieved_at


def test_offline_chain_end_to_end_is_traceable(store):
    run = ask("List bioactivity assay records for aspirin from ChEMBL.", provider="offline")
    assert run.tool_rounds() == [["search_chembl_molecule"], ["get_bioactivities"]]
    assert verify_run(run, store).ok


@pytest.mark.lotus
def test_lotus_live(store):
    by_taxon = search_by_taxon("Artemisia annua", limit=5)
    assert by_taxon.status is Status.OK, by_taxon.error
    assert any(r["references"] for r in by_taxon.records)

    none = search_by_taxon("Zzzznotaspecies annua", limit=3)
    assert none.status is Status.NO_MATCH, none.error

    occurrences = search_occurrences("BLUAFEHZUWYNDE-NNWCWBAJSA-N", limit=5)
    assert occurrences.status is Status.OK, occurrences.error
    assert all(r["taxon_name"] for r in occurrences.records)
