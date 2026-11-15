"""Tool definitions offered to the model, and the whitelist that executes them.

``TOOL_SPECS`` is the vendor-neutral description (JSON Schema). Every property
is listed in ``required`` and ``additionalProperties`` is false: Anthropic's
strict mode needs that, and it is harmless for OpenAI-compatible endpoints.
Optional parameters are expressed as ``["type", "null"]``; a null is replaced
by the default in ``DEFAULTS`` before the call.

An ``enum`` must not be combined with a union type. With
``{"type": ["string", "null"], "enum": [...]}`` strict mode rejects the schema
because each enum value is checked against the declared type, so ``sort`` is a
required single-type parameter.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

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
from sci_evidence_agent.providers.base import ToolSpec
from sci_evidence_agent.schemas import ToolResult


def _schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _limit(default: int, what: str = "records") -> dict[str, Any]:
    return {
        "type": ["integer", "null"],
        "description": f"Maximum number of {what} to return; default {default}",
    }


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="search_compound",
        description=(
            "Look up a compound in PubChem by name or CID. Returns the CID, molecular "
            "formula, SMILES, InChIKey, molecular weight and a source link. Use this "
            "for questions about chemical structure."
        ),
        parameters=_schema(
            {
                "query": {
                    "type": "string",
                    "description": 'Compound name (e.g. "aspirin") or a numeric CID (e.g. "2244")',
                },
                "limit": _limit(10),
            }
        ),
    ),
    ToolSpec(
        name="search_chembl_molecule",
        description=(
            "Search ChEMBL molecules by name or ChEMBL ID. Returns the ChEMBL ID, "
            "preferred name, molecular formula and structure identifiers. Call this "
            "first to obtain the ChEMBL ID needed for bioactivity records."
        ),
        parameters=_schema(
            {
                "query": {
                    "type": "string",
                    "description": 'Molecule name (e.g. "aspirin") or ChEMBL ID (e.g. "CHEMBL25")',
                },
                "limit": _limit(10),
            }
        ),
    ),
    ToolSpec(
        name="get_bioactivities",
        description=(
            "Fetch the bioactivity records of a molecule from ChEMBL. Returns target "
            "ID, assay ID, measurement type (IC50/Ki/EC50 ...), value, units and "
            "source. Requires a ChEMBL ID."
        ),
        parameters=_schema(
            {
                "chembl_id": {
                    "type": "string",
                    "description": 'ChEMBL ID of the molecule (e.g. "CHEMBL25")',
                },
                "limit": _limit(20, "activity records"),
            }
        ),
    ),
    ToolSpec(
        name="search_protein",
        description=(
            "Search UniProt by protein name, gene name or accession. Returns the "
            "accession, protein name, gene names, organism and source. Only reviewed "
            "Swiss-Prot entries are searched by default."
        ),
        parameters=_schema(
            {
                "query": {
                    "type": "string",
                    "description": (
                        'Protein name, gene name (e.g. "gene:EGFR") or accession (e.g. "P00533")'
                    ),
                },
                "organism_id": {
                    "type": ["integer", "null"],
                    "description": "NCBI taxonomy ID; human is 9606. Null for all organisms",
                },
                "reviewed_only": {
                    "type": ["boolean", "null"],
                    "description": "Only Swiss-Prot (reviewed) entries; default true",
                },
                "limit": _limit(10),
            }
        ),
    ),
    ToolSpec(
        name="search_literature",
        description=(
            "Search PubMed. Returns PMID, title, authors, journal, publication date and "
            "DOI. PubMed query syntax is supported, e.g. 'aspirin AND cyclooxygenase'. "
            "Use this whenever literature support is needed; never write citations "
            "from memory."
        ),
        parameters=_schema(
            {
                "term": {"type": "string", "description": "PubMed search expression"},
                "limit": {
                    "type": ["integer", "null"],
                    "description": "Maximum number of records to return; default 10, at most 100",
                },
                "sort": {
                    "type": "string",
                    "enum": ["relevance", "pub_date"],
                    "description": "Sort order: relevance or pub_date (publication date)",
                },
            }
        ),
    ),
    ToolSpec(
        name="get_article",
        description=(
            "Fetch the metadata of one PubMed article by PMID (title, authors, journal, date, DOI)."
        ),
        parameters=_schema(
            {
                "pmid": {
                    "type": "string",
                    "description": 'PubMed ID, digits only (e.g. "14592543")',
                },
            }
        ),
    ),
    ToolSpec(
        name="search_by_taxon",
        description=(
            "List natural products reported in a plant or other organism (LOTUS data). "
            "Returns compound name, InChIKey, SMILES and the literature references "
            "(DOI/PMID) for each structure-organism pair. Use this for 'which "
            "compounds does this plant contain'. The taxon must be a scientific "
            "(Latin) name."
        ),
        parameters=_schema(
            {
                "taxon": {
                    "type": "string",
                    "description": 'Scientific name, e.g. "Artemisia annua" or "Panax ginseng"',
                },
                "limit": _limit(20, "compounds"),
            }
        ),
    ),
    ToolSpec(
        name="search_occurrences",
        description=(
            "List the organisms in which a compound has been reported (LOTUS data). "
            "Requires the full InChIKey. Use this for 'which other plants contain "
            "this compound'."
        ),
        parameters=_schema(
            {
                "inchikey": {
                    "type": "string",
                    "description": 'Full 27-character InChIKey, e.g. "BLUAFEHZUWYNDE-NNWCWBAJSA-N"',
                },
                "limit": _limit(20, "taxa"),
            }
        ),
    ),
]

SPEC_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}

# Whitelist: a tool name proposed by the model must be in here or it is rejected.
REGISTRY: dict[str, Callable[..., ToolResult]] = {
    "search_compound": search_compound,
    "search_chembl_molecule": search_chembl_molecule,
    "get_bioactivities": get_bioactivities,
    "search_protein": search_protein,
    "search_literature": search_literature,
    "get_article": get_article,
    "search_by_taxon": search_by_taxon,
    "search_occurrences": search_occurrences,
}

# Defaults that replace a null (or a missing optional argument).
DEFAULTS: dict[str, dict[str, Any]] = {
    "search_compound": {"limit": 10},
    "search_chembl_molecule": {"limit": 10},
    "get_bioactivities": {"limit": 20},
    "search_protein": {"organism_id": None, "reviewed_only": True, "limit": 10},
    "search_literature": {"limit": 10, "sort": "relevance"},
    "get_article": {},
    "search_by_taxon": {"limit": 20},
    "search_occurrences": {"limit": 20},
}
