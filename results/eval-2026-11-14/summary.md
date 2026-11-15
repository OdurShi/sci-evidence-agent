# Evaluation summary: sci-evidence-bench v1.0

- Started (UTC): 2026-11-14T22:47:04+00:00; finished (UTC): 2026-11-14T22:49:39+00:00
- Total runtime: 154.8 s; mode: live; provider: offline
- Questions: 34; passes: 2; HTTP requests per pass: {'pass1_cold': 53, 'pass2_warm': 2}
- The first pass never reads the cache (every question is timed cold); later passes reuse what the first pass stored.

## Pass `pass1_cold`

| category | n | tool selection (exact) | tool precision | tool recall | status match | identifier recall | traceable share | handling of non-ok states | fabrication detected | latency median (s) | latency p90 (s) | cache hit rate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| compound | 6 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% (10/10) | 100.0% (12/12) | 100.0% (2/2) | 100.0% | 0.42 | 0.72 | 0.0% (0/6) |
| bioactivity_chain | 7 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% (5/5) | 99.0% (99/100) | 100.0% (3/3) | 100.0% | 0.59 | 1.11 | 0.0% (0/10) |
| protein | 7 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (9/9) | 100.0% (3/3) | 100.0% | 0.17 | 0.71 | 0.0% (0/7) |
| literature | 7 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% (11/11) | 96.5% (28/29) | 100.0% (2/2) | 100.0% | 1.16 | 1.42 | 0.0% (0/8) |
| cross_source | 7 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% (9/9) | 100.0% (84/84) | 100.0% (1/1) | 100.0% | 2.02 | 64.12 | 0.0% (0/14) |
| overall | 34 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% (41/41) | 99.2% (232/234) | 100.0% (11/11) | 100.0% | 0.69 | 1.92 | 0.0% (0/45) |

## Pass `pass2_warm`

| category | n | tool selection (exact) | tool precision | tool recall | status match | identifier recall | traceable share | handling of non-ok states | fabrication detected | latency median (s) | latency p90 (s) | cache hit rate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| compound | 6 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% (10/10) | 100.0% (12/12) | 100.0% (2/2) | 100.0% | 0.00 | 0.00 | 100.0% (6/6) |
| bioactivity_chain | 7 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% (5/5) | 99.0% (99/100) | 100.0% (3/3) | 100.0% | 0.00 | 0.01 | 100.0% (10/10) |
| protein | 7 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% (6/6) | 100.0% (9/9) | 100.0% (3/3) | 100.0% | 0.00 | 0.27 | 85.7% (6/7) |
| literature | 7 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% (11/11) | 96.5% (28/29) | 100.0% (2/2) | 100.0% | 0.00 | 0.00 | 100.0% (8/8) |
| cross_source | 7 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% (9/9) | 100.0% (84/84) | 100.0% (1/1) | 100.0% | 0.01 | 0.27 | 92.9% (13/14) |
| overall | 34 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% (41/41) | 99.2% (232/234) | 100.0% (11/11) | 100.0% | 0.00 | 0.01 | 95.6% (43/45) |

Answers identical between the first and last pass: 34/34

## Held-out paraphrases (planning only, no network)

Tool rounds exact: 18/20 (90.0%)

Tool rounds and arguments exact: 10/20 (50.0%)

| id | category | tools | arguments | planned calls | question |
|---|---|---|---|---|---|
| H01 | compound | yes | no | `search_compound(string)` | I need the SMILES string for ibuprofen. |
| H02 | compound | yes | no | `search_compound(caffeine weighs)` | Can you tell me how much caffeine weighs (molecular weight)? |
| H03 | compound | yes | yes | `search_compound(vitamin C)` | PubChem entry for vitamin C please |
| H04 | compound | yes | yes | `search_compound(3672)` | Molecular formula for CID 3672? |
| H05 | compound | no | no | `(no call)` | 阿司匹林的分子式是什么？ |
| H06 | bioactivity_chain | yes | no | `search_chembl_molecule(potent); get_bioactivities()` | How potent is imatinib? Show its IC50 measurements from ChEMBL. |
| H07 | bioactivity_chain | yes | yes | `get_bioactivities(CHEMBL521)` | Assay results for CHEMBL521 |
| H08 | bioactivity_chain | yes | no | `search_chembl_molecule(metformin against); get_bioactivities()` | What does ChEMBL know about the activity of metformin against its targets? |
| H09 | protein | yes | no | `search_protein(BRCA1 encode, 9606)` | Which protein does the human gene BRCA1 encode? Give the UniProt accession. |
| H10 | protein | yes | yes | `search_protein(insulin, 10116)` | UniProt record for rat insulin |
| H11 | protein | yes | yes | `search_protein(Q9BYF1, None)` | Tell me about UniProt entry Q9BYF1. |
| H12 | literature | yes | yes | `search_literature(CRISPR base editing)` | Recent papers on CRISPR base editing |
| H13 | literature | no | no | `search_literature(Pull)` | Pull up PubMed ID 21376230. |
| H14 | literature | yes | no | `search_literature(linking metformin AND longevity)` | Any publications linking metformin to longevity? |
| H15 | literature | yes | yes | `get_article(5284360)` | Get the DOI for the article with PMID 5284360 |
| H16 | cross_source | yes | yes | `search_by_taxon(Panax ginseng)` | What compounds are found in Panax ginseng? |
| H17 | cross_source | yes | yes | `search_compound(quercetin); search_occurrences()` | Is quercetin a natural product? Which organisms produce it? |
| H18 | cross_source | yes | no | `search_compound(Compare) + search_chembl_molecule(Compare); get_bioactivities()` | Compare what PubChem and ChEMBL have on sildenafil, including bioassay activities. |
| H19 | cross_source | yes | no | `search_protein(gene:PTGS2, 9606) + search_literature(PTGS2 AND celecoxib)` | Find the protein entry for PTGS2 in human and some literature about celecoxib. |
| H20 | cross_source | yes | yes | `search_compound(paracetamol) + search_chembl_molecule(paracetamol); get_bioactivities()` | Bioactivity of paracetamol in ChEMBL and its PubChem identifiers |
