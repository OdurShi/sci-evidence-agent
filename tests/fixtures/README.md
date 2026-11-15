# Test fixtures

Recorded HTTP responses used by the offline test suite. One JSON file per
exchange: the request, the status code, a few response headers and the body.

- **Recorded on 2026-11-14** from the live public APIs with
  `scripts/record_fixtures.py` (run it again to refresh them).
- **Trimmed.** Bodies keep the fields the connectors read plus a little
  context; long lists are cut to a few entries. The PubChem and PubMed
  `esearch` bodies are verbatim. Trimming is done by the recording script, so
  it is reproducible.
- **Not test oracles for the databases.** They pin down the response *shapes*
  the connectors rely on. Counts, rankings and release numbers will drift.

## Sources and licences

The files contain small excerpts of third-party data. They remain under the
terms of their sources, not under this repository's MIT licence.

| Directory | Source | Terms of the data |
|---|---|---|
| `pubchem/` | PubChem PUG REST, National Center for Biotechnology Information (NCBI) | NCBI places no restrictions on the use or distribution of PubChem data; some depositors may claim rights in records they submitted. See the NCBI policies: <https://www.ncbi.nlm.nih.gov/home/about/policies/> |
| `chembl/` | ChEMBL web services, EMBL-EBI | Creative Commons Attribution-ShareAlike 3.0 Unported (CC BY-SA 3.0): <https://creativecommons.org/licenses/by-sa/3.0/> |
| `uniprot/` | UniProtKB REST API, UniProt Consortium | Creative Commons Attribution 4.0 International (CC BY 4.0): <https://www.uniprot.org/help/license> |
| `pubmed/` | NCBI E-utilities (PubMed), U.S. National Library of Medicine | Bibliographic metadata only (no abstracts). NLM terms: <https://www.nlm.nih.gov/databases/download/terms_and_conditions.html>; E-utilities usage policy: <https://www.ncbi.nlm.nih.gov/books/NBK25497/> |
| `lotus/` | LOTUS data in Wikidata, via the Wikidata Query Service and the Wikidata Action API | Creative Commons CC0 1.0: <https://www.wikidata.org/wiki/Wikidata:Licensing> |
| `synthetic/` | Written by hand for this repository | MIT, like the code |

Because the `chembl/` files are CC BY-SA 3.0, anyone redistributing them must
keep that licence for those files and attribute ChEMBL.

## Files

`pubchem/`

| File | What it shows |
|---|---|
| `aspirin_by_name.json` | name lookup, one CID (`ok`) |
| `cid_2244.json` | CID lookup (`ok`) |
| `name_not_found.json` | HTTP 404 with `PUGREST.NotFound` (`no_match`) |
| `stearate_two_cids.json` | one name, two CIDs (`ambiguous`) |
| `cid_without_record.json` | HTTP 200 with a row that holds only the CID (`no_match`) |

`chembl/`

| File | What it shows |
|---|---|
| `molecule_search_aspirin.json` | search with an exact preferred-name match (`ok`), 52 hits upstream |
| `molecule_search_paracetamol.json` | search without an exact name match (`ambiguous`) |
| `molecule_search_none.json` | empty search (`no_match`) |
| `molecule_CHEMBL25.json` | direct fetch by ChEMBL ID (`ok`) |
| `molecule_unknown_id.json` | HTTP 404 with an HTML body (`no_match`) |
| `activity_CHEMBL25.json` | activity records, including a null `standard_units` (`ok`) |
| `activity_none.json` | empty activity list (`no_match`) |

`uniprot/`

| File | What it shows |
|---|---|
| `search_egfr_human.json` | one hit (`ok`) |
| `search_egfr_all_organisms.json` | 3 of 6 hits; the total is only in `X-Total-Results` (`ambiguous`) |
| `search_none.json` | empty search (`no_match`) |
| `search_bad_field.json` | HTTP 400 for a malformed query (`error`) |
| `search_or_parenthesised.json`, `search_or_unparenthesised.json` | why the connector wraps the query in parentheses: without them the organism filter binds to the last term only |
| `entry_P00533.json` | direct fetch by accession (`ok`) |
| `entry_inactive_Q9ZZZ9.json` | HTTP 200 with `entryType: Inactive` (`no_match`) |
| `entry_secondary_P06268.json` | a secondary accession resolved to its current entry |

`pubmed/`

| File | What it shows |
|---|---|
| `esearch_aspirin_cyclooxygenase.json`, `esummary_aspirin_cyclooxygenase.json` | the two requests of one search (`ok`) |
| `esearch_none.json` | HTTP 200 with `count: "0"` (`no_match`) |
| `esearch_unknown_field_tag.json` | an unknown field tag is dropped silently; `errorlist` is present but empty |
| `esearch_empty_term.json` | HTTP 200 with an `ERROR` string (`error`) |
| `esummary_14592543.json` | one article (`ok`) |
| `esummary_invalid_pmid.json` | HTTP 200 with an `error` field inside the record (`no_match`) |
| `esummary_not_a_number.json` | HTTP 200 with a top-level `error` (`no_match`) |

`lotus/`

| File | What it shows |
|---|---|
| `sparql_taxon_artemisia_annua.json`, `entities_taxon_artemisia_annua.json` | compounds reported in a taxon, and the Wikidata items that hold their labels and references (`ok`) |
| `sparql_occurrences_artemisinin.json`, `entities_occurrences_artemisinin.json` | taxa in which one compound was reported (`ok`, truncated) |
| `sparql_taxon_none.json` | empty SPARQL result (`no_match`) |

`synthetic/` (hand-written; each file carries `"synthetic": true` and a note)

| File | What it stands for |
|---|---|
| `pubchem_503_busy.json` | overload page: HTTP 503 with HTML |
| `chembl_500.json` | generic server error |
| `pubmed_429.json` | rate limit with a short `Retry-After` |
| `wikidata_429_blocked.json` | usage block: HTTP 429 with `Retry-After: 1000`, as observed on 2026-11-14; the page text was rewritten and the client address replaced by a documentation address |
| `wikidata_504.json` | gateway timeout for a heavy query |

`ambiguous` does not exist for PubMed, LOTUS and `get_bioactivities`: a
literature search returns a ranked list, and the other two work on one
identifier. `error` responses of PubChem, ChEMBL and the Wikidata services
cannot be provoked on demand, which is why they are synthetic.
