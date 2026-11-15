"""Database connectors. Every function returns a uniform ``ToolResult``."""

from sci_evidence_agent.connectors.chembl import get_bioactivities, search_chembl_molecule
from sci_evidence_agent.connectors.lotus import search_by_taxon, search_occurrences
from sci_evidence_agent.connectors.pubchem import search_compound
from sci_evidence_agent.connectors.pubmed import get_article, search_literature
from sci_evidence_agent.connectors.uniprot import search_protein

__all__ = [
    "get_article",
    "get_bioactivities",
    "search_by_taxon",
    "search_chembl_molecule",
    "search_compound",
    "search_literature",
    "search_occurrences",
    "search_protein",
]
