"""Citation verifier: is every identifier in an answer backed by a tool result?

Inventing identifiers is the most common and most dangerous failure of a
language model in a research setting: a made-up PMID, DOI or accession has a
perfectly valid shape and survives casual proofreading. This check is
mechanical, so it is automated.

Method: extract every identifier from the answer and compare it with the
identifiers found in the *archived raw responses* of the tools that were
called for that answer (the complete responses, not the few records shown to
the user). Each identifier ends up in one of three classes:

``traceable``    it occurs in an archived raw response of this run;
``echoed``       it does not, but the user supplied it (it occurs in the
                 question or in a tool argument), e.g. "PMID 999999999: no match";
``untraceable``  neither. It may be invented and needs a manual check.

Identifier kinds and how they are recognised are described in
:mod:`sci_evidence_agent.identifiers`. Two details matter for robustness:

* CIDs and PMIDs are bare integers. In the raw responses they are only
  accepted from the right source: a CID must be a whole integer value in a
  PubChem response, a PMID a whole integer value in a PubMed or LOTUS
  response. A number that merely occurs somewhere (a year, an activity ID)
  does not make a PMID traceable.
* Bare 7-8 digit numbers without a label are reported separately as
  *possible PMIDs* when no PubMed or LOTUS response contains them. They do
  not fail the check by default (``strict=True`` makes them count).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sci_evidence_agent.identifiers import (
    CHEMBL_RE,
    DOI_RE,
    INCHIKEY_RE,
    KINDS,
    UNIPROT_RE,
    clean_doi,
    looks_like_formula,
)

TRACEABLE = "traceable"
ECHOED = "echoed"
UNTRACEABLE = "untraceable"

# Sources whose raw responses can vouch for a numeric identifier.
CID_SOURCES = frozenset({"PubChem"})
PMID_SOURCES = frozenset({"PubMed", "LOTUS"})

_SEP = r"[\s:#=*`\[(\"']*"
_END = r"(?!\d|\.\d)"  # the number must end here: no further digit, no decimal part
_LIST = rf"(?:\s*(?:,|;|/|and|&|、|和)\s*\d{{1,10}}{_END})*"


def _label_pattern(label: str) -> re.Pattern[str]:
    """``<label> 123``; a plural label (``CIDs 1, 2 and 3``) may introduce a list."""
    return re.compile(
        rf"(?<![A-Za-z])(?:{label}s{_SEP}(?P<many>\d{{1,10}}{_END}{_LIST})"
        rf"|{label}{_SEP}(?P<one>\d{{1,10}}){_END})",
        re.I,
    )


_CID_LABEL = _label_pattern(r"CID")
_PMID_LABEL = _label_pattern(r"(?:PMID|PubMed\s+ID)")
_CID_URL = re.compile(r"pubchem\.ncbi\.nlm\.nih\.gov/compound/(\d{1,10})", re.I)
_PMID_URL = re.compile(
    r"(?:pubmed\.ncbi\.nlm\.nih\.gov/|ncbi\.nlm\.nih\.gov/pubmed/)(\d{1,9})", re.I
)
_WIKIDATA_QID = re.compile(r"(?:wikidata\.org/(?:wiki|entity)/|Wikidata\s+)Q\d+", re.I)
_UNIPROT_CONTEXT = re.compile(r"(?:uniprot|accession)[^\n]{0,40}$", re.I)
_BARE_NUMBER = re.compile(r"(?<![\w./:-])\d{7,8}(?![\w-]|\.\d)")
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")


@dataclass(frozen=True, order=True)
class Identifier:
    """A typed, normalised identifier."""

    kind: str
    value: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


def _blank(text: str, pattern: re.Pattern[str]) -> str:
    return pattern.sub(lambda m: " " * len(m.group(0)), text)


def _table_identifiers(text: str) -> set[Identifier]:
    """Numbers in Markdown table columns headed CID or PMID."""
    found: set[Identifier] = set()
    columns: dict[int, str] = {}
    for line in text.splitlines():
        m = _TABLE_ROW.match(line)
        if not m:
            columns = {}
            continue
        cells = [c.strip().strip("*`_ ") for c in m.group(1).split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue  # the separator row
        header = {
            i: kind
            for i, c in enumerate(cells)
            for kind, names in (("cid", ("cid", "pubchem cid")), ("pmid", ("pmid", "pubmed id")))
            if c.lower() in names
        }
        if header:
            columns = header
            continue
        for i, kind in columns.items():
            if i < len(cells):
                number = re.search(r"\d{1,10}", cells[i])
                if number:
                    found.add(Identifier(kind, number.group(0)))
    return found


def extract_identifiers(text: str) -> set[Identifier]:
    """All identifiers in free text (an answer, a question, a compact result)."""
    found: set[Identifier] = set()

    # URL-labelled numbers first, while the URLs are still intact.
    found |= {Identifier("cid", m.group(1)) for m in _CID_URL.finditer(text)}
    found |= {Identifier("pmid", m.group(1)) for m in _PMID_URL.finditer(text)}
    found |= _table_identifiers(text)

    # DOIs next; their suffixes can contain anything, so blank them afterwards.
    for m in DOI_RE.finditer(text):
        found.add(Identifier("doi", clean_doi(m.group(0))))
    work = _blank(text, DOI_RE)
    # A five-digit Wikidata QID (Q18216) has the shape of a UniProt accession.
    work = _blank(work, _WIKIDATA_QID)

    found |= {Identifier("inchikey", m.group(0)) for m in INCHIKEY_RE.finditer(work)}
    found |= {Identifier("chembl", m.group(0).upper()) for m in CHEMBL_RE.finditer(work)}

    for m in UNIPROT_RE.finditer(work):
        token = m.group(0)
        if looks_like_formula(token) and not _UNIPROT_CONTEXT.search(work[: m.start()]):
            continue  # C9H8O4 is a molecular formula, not an accession
        found.add(Identifier("uniprot", token))

    for kind, pattern in (("cid", _CID_LABEL), ("pmid", _PMID_LABEL)):
        for m in pattern.finditer(work):
            numbers = re.findall(r"\d+", m.group("many") or m.group("one"))
            found |= {Identifier(kind, n) for n in numbers}

    return found


def possible_pmids(text: str) -> set[str]:
    """Bare 7-8 digit numbers that are not already labelled identifiers."""
    labelled = {i.value for i in extract_identifiers(text) if i.kind in ("cid", "pmid")}
    work = _blank(_blank(text, DOI_RE), CHEMBL_RE)
    return {m.group(0) for m in _BARE_NUMBER.finditer(work)} - labelled


def _walk(node: Any, strings: list[str], numbers: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            strings.append(str(key))
            if str(key).isdigit():
                numbers.add(str(key))
            _walk(value, strings, numbers)
    elif isinstance(node, list):
        for item in node:
            _walk(item, strings, numbers)
    elif isinstance(node, bool) or node is None:
        return
    elif isinstance(node, int):
        numbers.add(str(node))
    elif isinstance(node, str):
        strings.append(node)
        if node.isdigit():
            numbers.add(node)


def identifiers_in_raw(response: Any, source: str) -> set[Identifier]:
    """Identifiers contained in one archived raw response.

    Shape-defined kinds are searched in every string of the response.
    Integers are typed by the source of the response (see module docstring).
    """
    strings: list[str] = []
    numbers: set[str] = set()
    _walk(response, strings, numbers)

    found = {i for i in extract_identifiers("\n".join(strings)) if i.kind not in ("cid", "pmid")}
    if source in CID_SOURCES:
        found |= {Identifier("cid", n) for n in numbers}
    if source in PMID_SOURCES:
        found |= {Identifier("pmid", n) for n in numbers}
    return found


@dataclass
class RawDocument:
    """One archived raw response (the ``response`` part of the envelope)."""

    source: str
    response: Any
    path: str | None = None


@dataclass
class VerificationReport:
    """Outcome of checking one answer."""

    findings: dict[Identifier, str] = field(default_factory=dict)
    possible_pmids_untraced: list[str] = field(default_factory=list)
    missing_raw: list[str] = field(default_factory=list)
    strict: bool = False

    def of(self, status: str) -> list[Identifier]:
        return sorted(i for i, s in self.findings.items() if s == status)

    @property
    def total(self) -> int:
        return len(self.findings)

    @property
    def untraceable(self) -> list[Identifier]:
        return self.of(UNTRACEABLE)

    @property
    def ok(self) -> bool:
        """True when nothing in the answer needs a manual check."""
        if self.strict and self.possible_pmids_untraced:
            return False
        return not self.untraceable

    def counts(self) -> dict[str, int]:
        tally = Counter(self.findings.values())
        return {
            "identifiers": self.total,
            TRACEABLE: tally[TRACEABLE],
            ECHOED: tally[ECHOED],
            UNTRACEABLE: tally[UNTRACEABLE],
        }

    def by_kind(self) -> dict[str, dict[str, int]]:
        table = {kind: {TRACEABLE: 0, ECHOED: 0, UNTRACEABLE: 0} for kind in KINDS}
        for identifier, status in self.findings.items():
            table[identifier.kind][status] += 1
        return table

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.counts(),
            "ok": self.ok,
            "untraceable_identifiers": [str(i) for i in self.untraceable],
            "echoed_identifiers": [str(i) for i in self.of(ECHOED)],
            "possible_pmids_untraced": self.possible_pmids_untraced,
            "missing_raw": self.missing_raw,
            "by_kind": self.by_kind(),
        }


def verify_answer(
    answer: str,
    raw_documents: list[RawDocument],
    echo_text: str = "",
    strict: bool = False,
) -> VerificationReport:
    """Classify every identifier in ``answer``.

    Args:
        answer: The text to check.
        raw_documents: Archived raw responses of the tool calls behind the answer.
        echo_text: The question plus the tool arguments; identifiers found
            here were supplied by the user, not recalled by the model.
        strict: Also fail on unlabelled 7-8 digit numbers that no PubMed or
            LOTUS response contains.
    """
    report = VerificationReport(strict=strict)

    universe: set[Identifier] = set()
    pmid_numbers: set[str] = set()
    for doc in raw_documents:
        ids = identifiers_in_raw(doc.response, doc.source)
        universe |= ids
        pmid_numbers |= {i.value for i in ids if i.kind == "pmid"}

    echoed = extract_identifiers(echo_text) if echo_text else set()

    for identifier in extract_identifiers(answer):
        if identifier in universe:
            report.findings[identifier] = TRACEABLE
        elif identifier in echoed:
            report.findings[identifier] = ECHOED
        else:
            report.findings[identifier] = UNTRACEABLE

    echo_numbers = set(re.findall(r"\d+", echo_text))
    report.possible_pmids_untraced = sorted(possible_pmids(answer) - pmid_numbers - echo_numbers)
    return report


def echo_text_for(question: str, arguments: list[dict[str, Any]]) -> str:
    """The text a user controls: the question and the arguments sent to tools."""
    return question + "\n" + "\n".join(json.dumps(a, ensure_ascii=False) for a in arguments)


def verify_run(run: Any, store: Any = None, strict: bool = False) -> VerificationReport:
    """Verify the answer of an :class:`~sci_evidence_agent.agent.AgentRun`.

    The raw responses are read from the provenance store through each
    result's ``raw_path``. A missing file is recorded in ``missing_raw``; the
    identifiers it would have vouched for then show up as untraceable.
    """
    from sci_evidence_agent import storage

    store = store or storage.get_store()
    documents: list[RawDocument] = []
    missing: list[str] = []
    for result in run.results:
        if not result.raw_path:
            continue
        envelope = store.read_raw(result.raw_path)
        if envelope is None:
            missing.append(result.raw_path)
            continue
        documents.append(RawDocument(result.source, envelope.get("response"), result.raw_path))

    arguments = [execution.arguments for execution in getattr(run, "executions", [])]
    report = verify_answer(
        run.answer, documents, echo_text_for(run.question, arguments), strict=strict
    )
    report.missing_raw = missing
    return report
