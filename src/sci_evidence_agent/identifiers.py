"""Shapes of the identifiers this package deals with.

Used by the planner (to recognise identifiers in a question) and by the
citation verifier (to find identifiers in an answer and in archived raw
responses), so both agree on what counts as an identifier.

====================  =====================================  ====================
kind                  example                                recognised by
====================  =====================================  ====================
``cid``               PubChem CID 2244                       context (see below)
``chembl``            CHEMBL25                               shape
``uniprot``           P00533, A0A024R161                     shape
``pmid``              PMID 14592543                          context (see below)
``doi``               10.1016/s0049-3848(03)00379-7          shape
``inchikey``          BSYNRYMUTXBXSQ-UHFFFAOYSA-N            shape
====================  =====================================  ====================

CIDs and PMIDs are plain integers. A bare number says nothing about what it
identifies, so they are only recognised next to a label ("CID 2244",
"PMID: 14592543"), inside a PubChem or PubMed URL, or in a table column
headed CID or PMID.
"""

from __future__ import annotations

import re

KINDS = ("cid", "chembl", "uniprot", "pmid", "doi", "inchikey")

INCHIKEY_RE = re.compile(r"(?<![A-Z0-9-])[A-Z]{14}-[A-Z]{10}-[A-Z](?![A-Z0-9])")
CHEMBL_RE = re.compile(r"(?<![A-Za-z0-9])CHEMBL\d+(?![A-Za-z0-9])", re.I)
UNIPROT_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})"
    r"(?![A-Za-z0-9])"
)
# A DOI suffix may contain almost anything; stop at whitespace, quotes, angle
# brackets and CJK punctuation, then trim trailing punctuation (clean_doi).
DOI_RE = re.compile(r"(?<![A-Za-z0-9.])10\.\d{4,9}/[^\s\"'<>|，。；、）】]+")

# Elements whose symbol is one upper-case letter. Accession-shaped tokens are
# upper-case only, so two-letter symbols cannot occur in them.
_ONE_LETTER_ELEMENTS = frozenset("HBCNOFPSKVYIWU")
_FORMULA_GROUP_RE = re.compile(r"([A-Z])(\d*)")


def looks_like_formula(token: str) -> bool:
    """True for upper-case molecular formulas such as ``C9H8O4``.

    Such tokens match the UniProt accession pattern by accident. A token is
    read as a formula when it splits completely into two or more
    element-count groups whose counts have no leading zero.
    """
    groups = _FORMULA_GROUP_RE.findall(token)
    if len(groups) < 2 or "".join(a + b for a, b in groups) != token:
        return False
    return all(
        symbol in _ONE_LETTER_ELEMENTS and not count.startswith("0") for symbol, count in groups
    )


_TRAILING = ".,;:!?'\"*`_"


def clean_doi(raw: str) -> str:
    """Normalise a DOI match: trim trailing punctuation and lower-case it.

    A closing parenthesis or bracket is only removed when it is unbalanced,
    because DOIs such as ``10.1016/s0049-3848(03)00379-7`` contain them.
    DOIs are case-insensitive, so the lower-case form is the canonical one.
    """
    doi = raw
    while True:
        trimmed = doi.rstrip(_TRAILING)
        for closer, opener in ((")", "("), ("]", "["), ("}", "{")):
            while trimmed.endswith(closer) and trimmed.count(closer) > trimmed.count(opener):
                trimmed = trimmed[:-1].rstrip(_TRAILING)
        if trimmed == doi:
            break
        doi = trimmed
    return doi.lower()
