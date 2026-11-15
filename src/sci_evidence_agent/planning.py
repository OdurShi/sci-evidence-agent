"""Rule-based planner: question text -> tool calls. No model is involved.

The planner exists for two reasons. Without any API key the whole pipeline
(tool selection, live queries, provenance, cache, logging, UI) can still run,
and it is a zero-cost, deterministic regression fixture: after changing a
connector or the agent loop, running it shows whether the pipeline still
works, without spending tokens and without model randomness.

It is deliberately simple and it does not understand language:

* Intents are detected from keyword lists (English and Chinese).
* Entities are typed identifiers found by regular expressions (CID, ChEMBL
  ID, UniProt accession, PMID, DOI, InChIKey), quoted phrases, gene-like
  tokens, scientific names (only for natural-product questions) and whatever
  ASCII words are left once stop words are removed.
* It never asks back. When a lookup is ambiguous it stops the chain and
  lists the candidates.

Planning is pure: :func:`plan_question` performs no I/O, so it can be tested
and evaluated without network access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sci_evidence_agent.identifiers import (
    CHEMBL_RE,
    DOI_RE,
    INCHIKEY_RE,
    UNIPROT_RE,
    clean_doi,
    looks_like_formula,
)

# -- intents ---------------------------------------------------------------------

INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "literature": (
        "文献", "论文", "引文", "参考", "发表",
        "pubmed", "literature", "paper", "papers", "article", "articles",
        "publication", "publications", "citation", "citations", "reference",
        "references", "pmid", "pmids", "doi", "journal",
    ),
    "activity": (
        "活性", "实验记录", "生物活性",
        "ic50", "ec50", "ki", "kd", "bioactivity", "bioactivities", "activity",
        "activities", "assay", "assays", "potency",
    ),
    "protein": (
        "蛋白", "基因", "靶点",
        "uniprot", "protein", "proteins", "gene", "genes", "accession",
        "swiss-prot", "swissprot", "trembl",
    ),
    "compound": (
        "化合物", "分子式", "结构", "分子量", "化学",
        "pubchem", "compound", "compounds", "molecular", "formula", "smiles",
        "inchikey", "inchi", "chemical", "cid",
    ),
    "natural_product": (
        "天然产物", "存在于",
        "lotus", "natural product", "natural products", "occurrence",
        "occurrences", "occur", "occurs", "found in", "isolated from", "in nature",
    ),
}  # fmt: skip


def _keyword_pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    """ASCII keywords match on word boundaries; CJK keywords match as substrings.

    Substring matching of ASCII keywords would make "ki" fire inside "kinase"
    and "gene" inside "general".
    """
    ascii_words = [re.escape(w) for w in words if w.isascii()]
    cjk_words = [re.escape(w) for w in words if not w.isascii()]
    parts = []
    if ascii_words:
        parts.append(r"(?<![A-Za-z0-9])(?:" + "|".join(ascii_words) + r")(?![A-Za-z0-9])")
    parts.extend(cjk_words)
    return re.compile("|".join(parts), re.I)


_INTENT_PATTERNS = {name: _keyword_pattern(words) for name, words in INTENT_KEYWORDS.items()}

# -- organisms -------------------------------------------------------------------

ORGANISMS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (9606, ("human", "humans", "homo sapiens", "人类", "人源")),
    (10090, ("mouse", "mice", "murine", "mus musculus", "小鼠")),
    (10116, ("rat", "rats", "rattus norvegicus", "大鼠")),
    (7955, ("zebrafish", "danio rerio", "斑马鱼")),
    (7227, ("fruit fly", "drosophila melanogaster", "果蝇")),
    (559292, ("yeast", "saccharomyces cerevisiae", "酵母")),
    (83333, ("e. coli", "escherichia coli", "大肠杆菌")),
    (3702, ("arabidopsis thaliana", "拟南芥")),
    (9913, ("bovine", "cow", "bos taurus")),
    (9823, ("pig", "sus scrofa")),
    (9031, ("chicken", "gallus gallus")),
)
_ORGANISM_PATTERNS = [(taxid, _keyword_pattern(names)) for taxid, names in ORGANISMS]
_ALL_ORGANISMS = _keyword_pattern(
    (
        "all species", "any species", "every species", "across species",
        "all organisms", "any organism", "every organism", "所有物种", "不限物种",
    )
)  # fmt: skip

DEFAULT_ORGANISM_ID = 9606  # the prototype always searched human proteins

# -- identifiers and tokens --------------------------------------------------------

PMID_RE = re.compile(
    r"\bPMIDs?\b[\s:#]*(\d{1,9}(?:\s*(?:,|and|&|、|和)\s*(?:PMID[\s:#]*)?\d{1,9})*)", re.I
)
CID_RE = re.compile(
    r"\bCIDs?\b[\s:#]*(\d{1,10}(?:\s*(?:,|and|&|、|和)\s*(?:CID[\s:#]*)?\d{1,10})*)", re.I
)
QUOTED_RE = re.compile(r"\"([^\"]{2,200})\"|“([^”]{2,200})”|「([^」]{2,200})」")
BINOMIAL_RE = re.compile(r"\b([A-Z][a-z]{2,})[ \t]+([a-z]{3,})\b")
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'-]*")
GENE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")

# Words that are never an entity. Simple inflections are stripped before the
# lookup (see _is_stopword).
_FUNCTION_WORDS = """
    a about across after all also am an and any are as at available be been being both but by
    can could did do does each either every for from had has have having her here his how i if
    in into is it its just like many may me might more most much must my no not of on one only
    or other our out over per please shall she should so some such than that the their them then
    there these they this those through to too under up us use using via was we were what when
    where whether which while who whom whose why will with within without would you your
"""
_REQUEST_VERBS = """
    check describe fetch find get give include keep know list look lookup match name need
    provide query report retrieve return search see show summarise summarize tell want
"""
_DOMAIN_NOUNS = """
    accession activity ai api article assay bioactivity chembl chemical cid citation compound
    data database detail doi drug entry formula gene id identifier inchi inchikey info
    information journal json literature lotus mass maximum measurement molecular molecule
    natural nature number occur occurrence organism paper pmid potency product protein
    publication pubchem pubmed record reference related relevant result sequence smiles source
    species structure swiss-prot swissprot symbol target taxon taxa title titled trembl type
    uniprot unit value weight length top max first latest recent known held hold recorded
    reported found isolated plant plants entries
"""
_MEASUREMENT_TYPES = """
    ic50 ec50 ki kd ac50 gi50 cc50 ld50 ed50
"""
STOPWORDS = frozenset(
    f"{_FUNCTION_WORDS} {_REQUEST_VERBS} {_DOMAIN_NOUNS} {_MEASUREMENT_TYPES}".split()
)


def _is_stopword(token: str) -> bool:
    low = token.lower()
    if low in STOPWORDS:
        return True
    for suffix, replacements in (("ies", ("y",)), ("es", ("", "e")), ("s", ("",)),
                                 ("ing", ("", "e")), ("ed", ("", "e"))):  # fmt: skip
        if low.endswith(suffix) and len(low) > len(suffix) + 1:
            stem = low[: -len(suffix)]
            if any(stem + r in STOPWORDS for r in replacements):
                return True
    return False


def _has_letter(token: str) -> bool:
    return any(ch.isalpha() for ch in token)


def is_gene_like(token: str) -> bool:
    """Upper-case symbol such as EGFR, PTGS1, TP53 (not a stop word)."""
    return bool(GENE_RE.match(token)) and (len(token) >= 3 or any(c.isdigit() for c in token))


# -- data structures ---------------------------------------------------------------


@dataclass
class Entities:
    """Everything the planner recognised in a question."""

    cids: list[str] = field(default_factory=list)
    chembl_ids: list[str] = field(default_factory=list)
    accessions: list[str] = field(default_factory=list)
    pmids: list[str] = field(default_factory=list)
    dois: list[str] = field(default_factory=list)
    inchikeys: list[str] = field(default_factory=list)
    quoted: list[str] = field(default_factory=list)
    taxa: list[str] = field(default_factory=list)
    genes: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    #: genes and names in order of appearance, used to build literature queries
    mentions: list[str] = field(default_factory=list)
    organism_id: int | None = None
    organism_stated: bool = False  # an organism (or "all species") was named

    def is_empty(self) -> bool:
        return not any(
            (self.cids, self.chembl_ids, self.accessions, self.pmids, self.dois,
             self.inchikeys, self.quoted, self.taxa, self.genes, self.names)
        )  # fmt: skip

    def labels(self) -> list[str]:
        """Human-readable list of recognised entities."""
        out: list[str] = []
        out += [f"CID {v}" for v in self.cids]
        out += self.chembl_ids + self.accessions
        out += [f"PMID {v}" for v in self.pmids]
        out += [f"DOI {v}" for v in self.dois]
        out += self.inchikeys
        out += [f'"{v}"' for v in self.quoted]
        out += self.taxa + self.mentions
        return out


@dataclass(frozen=True)
class Binding:
    """Fill ``argument`` from the first record of an earlier, unambiguous result."""

    argument: str
    from_tool: str
    record_field: str


@dataclass(frozen=True)
class Step:
    """One planned tool call."""

    tool: str
    arguments: dict[str, Any]
    binding: Binding | None = None


@dataclass
class Plan:
    """Tool calls grouped into rounds; calls within a round are independent."""

    rounds: list[list[Step]] = field(default_factory=list)
    entities: Entities = field(default_factory=Entities)
    intents: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def tool_names(self) -> list[list[str]]:
        return [[step.tool for step in steps] for steps in self.rounds]


# -- extraction --------------------------------------------------------------------


def detect_intents(question: str) -> list[str]:
    """Intents named by keywords; ``["compound"]`` when nothing matches."""
    hits = [name for name, pattern in _INTENT_PATTERNS.items() if pattern.search(question)]
    return hits or ["compound"]


def _blank(text: str, start: int, end: int) -> str:
    """Replace a span with a separator of the same length so offsets stay valid."""
    return text[:start] + "|" * (end - start) + text[end:]


def extract_entities(question: str, intents: list[str] | None = None) -> Entities:
    """Find typed identifiers, quoted phrases, organisms and free-text names."""
    intents = intents if intents is not None else detect_intents(question)
    ent = Entities()
    text = question

    def take(pattern: re.Pattern[str], sink: list[str], convert: Any = None) -> None:
        """Collect every match of ``pattern`` and blank it out of the text."""
        nonlocal text
        matches = list(pattern.finditer(text))
        for m in matches:
            found = convert(m) if convert else [m.group(0)]
            for value in found:
                if value and value not in sink:
                    sink.append(value)
        for m in reversed(matches):
            text = _blank(text, m.start(), m.end())

    def accession(m: re.Match[str]) -> list[str]:
        token = m.group(0)
        # C9H8O4 has the shape of an accession; keep it only in a protein question.
        return [] if looks_like_formula(token) and "protein" not in intents else [token]

    # Quoted phrases first: nothing inside quotes is interpreted further.
    take(QUOTED_RE, ent.quoted, lambda m: [next(g for g in m.groups() if g).strip()])
    take(DOI_RE, ent.dois, lambda m: [clean_doi(m.group(0))])
    take(INCHIKEY_RE, ent.inchikeys)
    take(CHEMBL_RE, ent.chembl_ids, lambda m: [m.group(0).upper()])
    take(PMID_RE, ent.pmids, lambda m: re.findall(r"\d+", m.group(1)))
    take(CID_RE, ent.cids, lambda m: re.findall(r"\d+", m.group(1)))
    take(UNIPROT_RE, ent.accessions, accession)

    # Organism: an explicit "all species" wins, then the first named organism.
    if _ALL_ORGANISMS.search(text):
        ent.organism_id, ent.organism_stated = None, True
        for m in reversed(list(_ALL_ORGANISMS.finditer(text))):
            text = _blank(text, m.start(), m.end())
    for taxid, pattern in _ORGANISM_PATTERNS:
        matches = list(pattern.finditer(text))
        if matches and not ent.organism_stated:
            ent.organism_id, ent.organism_stated = taxid, True
        for m in reversed(matches):
            text = _blank(text, m.start(), m.end())

    # Scientific names are only read as taxa in natural-product questions;
    # elsewhere "Genus species" stays an ordinary name (e.g. a literature topic).
    if "natural_product" in intents:
        for m in list(BINOMIAL_RE.finditer(text)):
            genus, epithet = m.group(1), m.group(2)
            if _is_stopword(genus) or _is_stopword(epithet):
                continue
            taxon = f"{genus} {epithet}"
            if taxon not in ent.taxa:
                ent.taxa.append(taxon)
            text = _blank(text, m.start(), m.end())

    # Remaining ASCII words: consecutive non-stop words form one phrase.
    phrases: list[list[str]] = []
    previous_end: int | None = None
    for m in TOKEN_RE.finditer(text):
        token = m.group(0).strip("'-")
        if not token or not _has_letter(token) or _is_stopword(token):
            previous_end = None
            continue
        adjacent = previous_end is not None and text[previous_end : m.start()].strip(" \t") == ""
        if len(token) < 3 and not is_gene_like(token):
            # Short tokens only count as the tail of a phrase ("vitamin C").
            if adjacent and len(token) <= 2:
                phrases[-1].append(token)
                previous_end = m.end()
            else:
                previous_end = None
            continue
        if adjacent:
            phrases[-1].append(token)
        else:
            phrases.append([token])
        previous_end = m.end()

    for words in phrases:
        phrase = " ".join(words)
        if len(words) == 1 and is_gene_like(words[0]):
            if phrase not in ent.genes:
                ent.genes.append(phrase)
        elif phrase not in ent.names:
            ent.names.append(phrase)
        if phrase not in ent.mentions:
            ent.mentions.append(phrase)

    return ent


# -- planning ----------------------------------------------------------------------

SEARCH_LIMIT = 5
ACTIVITY_LIMIT = 10
OCCURRENCE_LIMIT = 10
MAX_ARTICLES = 5


def _protein_query(ent: Entities, intents: list[str]) -> str | None:
    if ent.accessions:
        return ent.accessions[0]
    if ent.genes:
        return f"gene:{ent.genes[0]}"
    if "protein" not in intents:
        return None
    # A quoted phrase is used verbatim unless literature claims it.
    if ent.quoted and "literature" not in intents:
        return ent.quoted[0]
    if ent.names:
        name = ent.names[0]
        # A single mixed-case or digit-bearing token is read as a gene symbol
        # (Trp53); a plain word or a multi-word name is searched as free text.
        if " " not in name and (any(c.isdigit() for c in name) or any(c.isupper() for c in name)):
            return f"gene:{name}"
        return name
    return None


def _compound_name(ent: Entities, intents: list[str]) -> str | None:
    if ent.names:
        return ent.names[0]
    if ent.quoted and "literature" not in intents and "protein" not in intents:
        return ent.quoted[0]
    # Prototype behaviour: a lone gene-like token is still looked up as a
    # compound when the question is about compounds (e.g. "DMSO").
    if ent.genes and "protein" not in intents:
        return ent.genes[0]
    return None


def plan_question(question: str) -> Plan:
    """Turn a question into a plan. Pure function: no network, no state."""
    intents = detect_intents(question)
    ent = extract_entities(question, intents)
    plan = Plan(entities=ent, intents=intents)

    if ent.is_empty():
        return plan

    explicit = any(pattern.search(question) for pattern in _INTENT_PATTERNS.values())
    first: list[Step] = []
    second: list[Step] = []

    def add(steps: list[Step], step: Step) -> None:
        if not any(s.tool == step.tool and s.arguments == step.arguments for s in steps):
            steps.append(step)

    compound_name = _compound_name(ent, intents)

    # Compound lookup in PubChem.
    if "compound" in intents:
        if ent.cids:
            for cid in ent.cids[:3]:
                add(first, Step("search_compound", {"query": cid, "limit": SEARCH_LIMIT}))
        elif compound_name:
            add(first, Step("search_compound", {"query": compound_name, "limit": SEARCH_LIMIT}))

    # Protein lookup in UniProt. Without any intent keyword a gene-like token
    # is also tried as a protein (prototype behaviour).
    wants_protein = "protein" in intents or bool(ent.accessions) or (not explicit and ent.genes)
    protein_query = _protein_query(ent, intents) if wants_protein else None
    if protein_query:
        organism_id = ent.organism_id
        if not ent.organism_stated and not ent.accessions:
            organism_id = DEFAULT_ORGANISM_ID
            plan.notes.append(
                f"No organism was named; the protein search defaulted to human "
                f"(taxonomy ID {DEFAULT_ORGANISM_ID})."
            )
        add(
            first,
            Step(
                "search_protein",
                {
                    "query": protein_query,
                    "organism_id": organism_id,
                    "reviewed_only": True,
                    "limit": SEARCH_LIMIT,
                },
            ),
        )

    # Literature: explicit PMIDs are fetched, anything else is a search.
    if "literature" in intents:
        if ent.pmids:
            for pmid in ent.pmids[:MAX_ARTICLES]:
                add(first, Step("get_article", {"pmid": pmid}))
        else:
            if ent.dois:
                term = f"{ent.dois[0]}[doi]"
            elif ent.quoted:
                term = f'"{ent.quoted[0]}"'
            else:
                term = " AND ".join(ent.mentions)
            if term:
                add(
                    first,
                    Step(
                        "search_literature",
                        {"term": term, "limit": SEARCH_LIMIT, "sort": "relevance"},
                    ),
                )

    # Bioactivity needs a ChEMBL ID. With a name this takes two rounds, which
    # also exercises the multi-round loop and argument passing between rounds.
    if "activity" in intents:
        if ent.chembl_ids:
            add(
                first,
                Step(
                    "get_bioactivities", {"chembl_id": ent.chembl_ids[0], "limit": ACTIVITY_LIMIT}
                ),
            )
        elif compound_name:
            add(
                first,
                Step("search_chembl_molecule", {"query": compound_name, "limit": SEARCH_LIMIT}),
            )
            add(
                second,
                Step(
                    "get_bioactivities",
                    {"limit": ACTIVITY_LIMIT},
                    Binding("chembl_id", "search_chembl_molecule", "source_id"),
                ),
            )

    # Natural products (LOTUS): by taxon, by InChIKey, or name -> InChIKey -> taxa.
    if "natural_product" in intents:
        if ent.taxa:
            add(first, Step("search_by_taxon", {"taxon": ent.taxa[0], "limit": OCCURRENCE_LIMIT}))
        elif ent.inchikeys:
            add(
                first,
                Step(
                    "search_occurrences",
                    {"inchikey": ent.inchikeys[0], "limit": OCCURRENCE_LIMIT},
                ),
            )
        elif compound_name:
            add(first, Step("search_compound", {"query": compound_name, "limit": SEARCH_LIMIT}))
            add(
                second,
                Step(
                    "search_occurrences",
                    {"limit": OCCURRENCE_LIMIT},
                    Binding("inchikey", "search_compound", "inchikey"),
                ),
            )

    if first:
        plan.rounds.append(first)
    if second:
        plan.rounds.append(second)
    return plan
