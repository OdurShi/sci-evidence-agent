"""sci-evidence-agent: database-grounded answers with archived provenance.

A question is turned into tool calls (by a language model or by the offline
keyword planner), the tools query PubChem, ChEMBL, UniProt, PubMed and
LOTUS/Wikidata, every raw response is archived, and identifiers in the final
answer can be checked against that archive.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
