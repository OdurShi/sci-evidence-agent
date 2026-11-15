"""Result schema shared by every connector.

Whatever database is queried and whether or not the query succeeds, a
connector returns one :class:`ToolResult`. Callers (the agent loop, the
reporter, the UI) only need to know this one shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Status(str, Enum):
    """Outcome of a query. The four states must stay distinguishable."""

    OK = "ok"  # records were found
    NO_MATCH = "no_match"  # the query was valid but nothing matched
    AMBIGUOUS = "ambiguous"  # several candidates; the caller or user must choose
    ERROR = "error"  # the request failed: timeout, rate limit, server error, bad response


def utc_now() -> str:
    """Current UTC time as an ISO-8601 string with second resolution."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CompoundRecord(BaseModel):
    """A chemical compound. PubChem and ChEMBL both map to this record."""

    source_db: str
    source_id: str  # PubChem CID or ChEMBL ID
    name: str | None = None
    molecular_formula: str | None = None
    smiles: str | None = None
    inchikey: str | None = None
    molecular_weight: float | None = None
    source_url: str | None = None


class BioactivityRecord(BaseModel):
    """One bioactivity measurement. Missing values stay ``None``; never fill defaults."""

    source_db: str = "ChEMBL"
    activity_id: str | None = None
    molecule_chembl_id: str | None = None
    target_chembl_id: str | None = None
    target_name: str | None = None
    assay_chembl_id: str | None = None
    assay_type: str | None = None
    standard_type: str | None = None  # IC50 / Ki / EC50 ...
    standard_value: float | None = None
    standard_units: str | None = None  # null upstream for dimensionless measurements
    standard_relation: str | None = None
    source_url: str | None = None


class ProteinRecord(BaseModel):
    """A UniProtKB entry."""

    source_db: str = "UniProt"
    accession: str
    protein_name: str | None = None
    gene_names: list[str] = Field(default_factory=list)
    organism: str | None = None
    taxonomy_id: int | None = None
    reviewed: bool | None = None  # True = Swiss-Prot (manually reviewed)
    sequence_length: int | None = None
    source_url: str | None = None


class LiteratureRecord(BaseModel):
    """A PubMed article (metadata only)."""

    source_db: str = "PubMed"
    pmid: str
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    journal: str | None = None
    pub_date: str | None = None  # as returned by PubMed; the format varies, so it is not parsed
    doi: str | None = None
    doi_url: str | None = None
    source_url: str | None = None


class CitationRef(BaseModel):
    """A literature reference attached to a LOTUS structure-organism pair."""

    wikidata_id: str
    title: str | None = None
    doi: str | None = None
    pmid: str | None = None
    year: str | None = None
    url: str | None = None


class OccurrenceRecord(BaseModel):
    """A natural-product occurrence: compound X was reported in taxon Y.

    What sets LOTUS apart is that each pair is referenced. A pair without a
    reference is flagged and must not be treated as equivalent to a referenced one.
    """

    source_db: str = "LOTUS"
    wikidata_id: str  # Wikidata QID of the compound
    compound_name: str | None = None
    inchikey: str | None = None
    smiles: str | None = None
    taxon_name: str | None = None
    taxon_wikidata_id: str | None = None
    references: list[dict[str, Any]] = Field(default_factory=list)
    reference_count: int = 0
    source_url: str | None = None


AnyRecord = CompoundRecord | BioactivityRecord | ProteinRecord | LiteratureRecord | OccurrenceRecord


class ToolResult(BaseModel):
    """Uniform return value of every connector."""

    status: Status
    source: str  # "PubChem" / "ChEMBL" / "UniProt" / "PubMed" / "LOTUS"
    query: dict[str, Any]  # the arguments actually used
    records: list[dict[str, Any]] = Field(default_factory=list)
    source_url: str | None = None  # a URL a person can open to check the result
    retrieved_at: str = Field(default_factory=utc_now)  # original time, also for cache hits
    from_cache: bool = False
    truncated: bool = False  # True when ``limit`` cut the result short
    total_available: int | None = None  # total reported upstream, when known
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None  # reason when status is ``error``
    raw_path: str | None = None  # archived raw response, relative to the data directory

    def summary(self) -> str:
        """One-line summary shown to the model and in reports."""
        if self.status is Status.OK:
            n = len(self.records)
            if not self.truncated:
                extra = ""
            elif self.total_available is None:
                # Saying "unknown" is better than inventing a number.
                extra = " (truncated; upstream total unknown)"
            else:
                extra = f" ({self.total_available} available upstream; truncated)"
            return f"{self.source}: {n} record{'s' if n != 1 else ''}{extra}"
        if self.status is Status.NO_MATCH:
            return f"{self.source}: no matching record"
        if self.status is Status.AMBIGUOUS:
            return f"{self.source}: {len(self.records)} candidates; needs confirmation"
        # Upstream errors already start with the source name; do not repeat it.
        detail = (self.error or "").removeprefix(f"{self.source}: ")
        return f"{self.source}: request failed: {detail}"


class ToolCallLog(BaseModel):
    """Record of one tool call, rendered in the UI call log."""

    tool_name: str
    arguments: dict[str, Any]
    status: Status
    duration_ms: int
    from_cache: bool
    record_count: int
    called_at: str = Field(default_factory=utc_now)
    error: str | None = None


def error_result(source: str, query: dict[str, Any], message: str) -> ToolResult:
    """Build an ``error`` result. A failure must never look like "no data"."""
    return ToolResult(status=Status.ERROR, source=source, query=query, error=message)


def no_match_result(
    source: str,
    query: dict[str, Any],
    url: str | None = None,
    warnings: list[str] | None = None,
) -> ToolResult:
    """Build a ``no_match`` result."""
    return ToolResult(
        status=Status.NO_MATCH,
        source=source,
        query=query,
        source_url=url,
        warnings=list(warnings or []),
    )
