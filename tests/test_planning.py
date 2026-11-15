"""Offline planner: intents, entities and plans. Pure functions, no I/O."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sci_evidence_agent.identifiers import looks_like_formula
from sci_evidence_agent.planning import (
    Binding,
    detect_intents,
    extract_entities,
    is_gene_like,
    plan_question,
)

BENCHMARK = json.loads(
    (Path(__file__).parent.parent / "benchmark" / "questions.json").read_text(encoding="utf-8")
)["questions"]


def calls(question: str) -> list[list[tuple[str, dict]]]:
    return [[(s.tool, s.arguments) for s in steps] for steps in plan_question(question).rounds]


# -- the prototype's five demo questions (regression) -------------------------------


def test_demo_single_source_compound():
    assert calls("查询 aspirin 的化合物标识、分子式和结构信息。") == [
        [("search_compound", {"query": "aspirin", "limit": 5})]
    ]


def test_demo_two_step_chain():
    plan = plan_question(
        "在 ChEMBL 中检索 aspirin，列出最多 10 条活性实验记录，保留实验类型、数值和单位。"
    )
    assert plan.tool_names() == [["search_chembl_molecule"], ["get_bioactivities"]]
    assert plan.rounds[0][0].arguments == {"query": "aspirin", "limit": 5}
    second = plan.rounds[1][0]
    assert second.arguments == {"limit": 10}
    assert second.binding == Binding("chembl_id", "search_chembl_molecule", "source_id")


def test_demo_protein_with_organism():
    assert calls("查询人类 EGFR 的 UniProt 条目，返回蛋白名称、基因名称、物种和来源。") == [
        [
            (
                "search_protein",
                {"query": "gene:EGFR", "organism_id": 9606, "reviewed_only": True, "limit": 5},
            )
        ]
    ]


def test_demo_literature():
    assert calls("检索 aspirin 与环氧化酶相关的文献，列出 5 篇，带 PMID 和 DOI。") == [
        [("search_literature", {"term": "aspirin", "limit": 5, "sort": "relevance"})]
    ]


def test_demo_cross_source_single_entity():
    plan = plan_question("查询 PTGS1 的蛋白信息和相关文献，按数据库整理结果并附来源。")
    assert plan.tool_names() == [["search_protein", "search_literature"]]
    assert plan.rounds[0][0].arguments["query"] == "gene:PTGS1"
    assert plan.rounds[0][1].arguments["term"] == "PTGS1"
    assert "defaulted to human" in plan.notes[0]


def test_demo_cross_source_assigns_each_entity_to_its_database():
    # The prototype used one entity for every tool and searched UniProt for gene:aspirin.
    plan = plan_question(
        "分别查询 aspirin 的化学信息、人类 PTGS1 的蛋白信息，"
        "以及相关文献，按数据库整理结果并附来源。"
    )
    by_tool = {s.tool: s.arguments for s in plan.rounds[0]}
    assert by_tool["search_compound"]["query"] == "aspirin"
    assert by_tool["search_protein"]["query"] == "gene:PTGS1"
    assert by_tool["search_literature"]["term"] == "aspirin AND PTGS1"
    assert len(plan.rounds) == 1


# -- the benchmark's expected plans -----------------------------------------------------


@pytest.mark.parametrize("question", BENCHMARK, ids=[q["id"] for q in BENCHMARK])
def test_benchmark_plan(question):
    planned = [sorted(r) for r in plan_question(question["question"]).tool_names()]
    expected = [sorted(r) for r in question["expected_tools"]]
    # Where the expected run stops early (ambiguous / no_match in a chain), the
    # plan still holds the follow-up step; it is dropped at execution time.
    assert planned[: len(expected)] == expected
    if len(planned) > len(expected):
        statuses = [s for r in question["expected_statuses"] for s in r]
        assert set(statuses) & {"ambiguous", "no_match", "error"}


# -- intents ---------------------------------------------------------------------------


def test_ascii_keywords_match_whole_words_only():
    # Substring matching made "ki" fire inside "kinase" and "gene" inside "general".
    assert detect_intents("EGFR kinase domain in general") == ["compound"]
    assert detect_intents("Ki values for imatinib") == ["activity"]
    assert "protein" in detect_intents("the TP53 gene")
    assert detect_intents("particle skin generation") == ["compound"]


def test_default_intent_is_compound():
    assert detect_intents("aspirin") == ["compound"]
    assert calls("aspirin") == [[("search_compound", {"query": "aspirin", "limit": 5})]]


def test_cjk_keywords_match_as_substrings():
    assert detect_intents("查一下相关论文") == ["literature"]
    assert set(detect_intents("蛋白和活性")) == {"activity", "protein"}


# -- entities --------------------------------------------------------------------------


def test_typed_identifiers_are_recognised():
    ent = extract_entities(
        "Look up CID 2244 and CHEMBL25 with P00533, PMID: 14592543, "
        "doi 10.1016/s0049-3848(03)00379-7. and BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
        ["protein"],
    )
    assert ent.cids == ["2244"]
    assert ent.chembl_ids == ["CHEMBL25"]
    assert ent.accessions == ["P00533"]
    assert ent.pmids == ["14592543"]
    assert ent.dois == ["10.1016/s0049-3848(03)00379-7"]  # trailing full stop removed
    assert ent.inchikeys == ["BSYNRYMUTXBXSQ-UHFFFAOYSA-N"]
    assert ent.names == [] and ent.genes == []  # nothing left over


def test_pmid_lists():
    assert extract_entities("PMIDs 1, 22 and 333").pmids == ["1", "22", "333"]
    assert extract_entities("PMID 34265844 and PMID 5284360").pmids == ["34265844", "5284360"]


def test_molecular_formula_is_not_an_accession():
    assert looks_like_formula("C9H8O4") and looks_like_formula("C8H10N4O2")
    assert not looks_like_formula("P00533") and not looks_like_formula("O60885")
    assert not looks_like_formula("Q9Y6K9")
    assert extract_entities("which compound is C9H8O4", ["compound"]).accessions == []


def test_multi_word_names_and_trailing_short_tokens():
    assert extract_entities("the formula of acetylsalicylic acid").names == ["acetylsalicylic acid"]
    assert extract_entities("PubChem entry for vitamin C").names == ["vitamin C"]
    assert extract_entities("aspirin 与 cyclooxygenase").names == ["aspirin", "cyclooxygenase"]


def test_gene_like_tokens():
    assert is_gene_like("EGFR") and is_gene_like("TP53") and is_gene_like("F8")
    assert not is_gene_like("aspirin") and not is_gene_like("Trp53") and not is_gene_like("AB")
    ent = extract_entities("aspirin and PTGS1")
    assert (ent.names, ent.genes, ent.mentions) == (["aspirin"], ["PTGS1"], ["aspirin", "PTGS1"])
    # Measurement types look like gene symbols but are stop words.
    assert extract_entities("IC50 and EC50 of imatinib").genes == []


def test_quoted_phrase_is_taken_verbatim():
    ent = extract_entities('the protein "kinase: human" please', ["protein"])
    assert ent.quoted == ["kinase: human"]
    assert ent.organism_stated is False  # nothing inside the quotes is interpreted


@pytest.mark.parametrize(
    ("text", "taxid"),
    [
        ("mouse gene Trp53", 10090),
        ("rat insulin protein", 10116),
        ("human EGFR", 9606),
        ("人类 EGFR 蛋白", 9606),
        ("Homo sapiens TP53", 9606),
        ("EGFR protein across all species", None),
    ],
)
def test_organism_detection(text, taxid):
    ent = extract_entities(text, ["protein"])
    assert ent.organism_stated is True and ent.organism_id == taxid


def test_scientific_name_is_a_taxon_only_in_natural_product_questions():
    np_plan = plan_question("Which natural products are found in Panax ginseng?")
    assert np_plan.entities.taxa == ["Panax ginseng"]
    assert np_plan.tool_names() == [["search_by_taxon"]]
    lit_plan = plan_question("papers about Panax ginseng")
    assert lit_plan.entities.taxa == []
    assert lit_plan.rounds[0][0].arguments["term"] == "Panax ginseng"
    # Capitalised sentence starts are not genera.
    assert plan_question("Which natural products occur in nature?").entities.taxa == []


# -- plans -----------------------------------------------------------------------------


def test_chembl_id_skips_the_search_step():
    assert calls("bioactivities of CHEMBL941") == [
        [("get_bioactivities", {"chembl_id": "CHEMBL941", "limit": 10})]
    ]


def test_accession_is_looked_up_without_organism_filter():
    step = plan_question("UniProt accession P04637").rounds[0][0]
    assert step.arguments["query"] == "P04637" and step.arguments["organism_id"] is None


def test_lowercase_protein_name_is_free_text_but_mixed_case_is_a_gene_symbol():
    assert plan_question("UniProt protein insulin").rounds[0][0].arguments["query"] == "insulin"
    assert plan_question("UniProt gene Trp53").rounds[0][0].arguments["query"] == "gene:Trp53"


def test_doi_and_pmid_literature_plans():
    assert calls("PubMed record for DOI 10.1016/j.cell.2011.02.013?") == [
        [
            (
                "search_literature",
                {"term": "10.1016/j.cell.2011.02.013[doi]", "limit": 5, "sort": "relevance"},
            )
        ]
    ]
    assert calls("PubMed articles PMID 1 and PMID 2") == [
        [("get_article", {"pmid": "1"}), ("get_article", {"pmid": "2"})]
    ]


def test_natural_product_chain_binds_the_inchikey():
    plan = plan_question("Where does artemisinin occur in nature?")
    assert plan.tool_names() == [["search_compound"], ["search_occurrences"]]
    assert plan.rounds[1][0].binding == Binding("inchikey", "search_compound", "inchikey")


def test_no_entity_means_no_plan():
    plan = plan_question("阿司匹林的分子式是什么？")
    assert plan.rounds == [] and plan.entities.is_empty()
    assert plan_question("").rounds == []


def test_planning_is_deterministic():
    question = "Give me the PubChem record for imatinib and its ChEMBL bioactivity data."
    assert plan_question(question) == plan_question(question)
