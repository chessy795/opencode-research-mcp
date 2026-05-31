# research-mcp

A research plugin that improves on the two best existing academic search plugins (academix and paper-search) by adding relevance scoring, citation-weighted ranking, source filtering, and automated citation walking.

**What it does:** You ask a research question, it searches 8 academic databases at once, removes duplicates, ranks results by how well they match your query, and returns the best 15 papers with a relevance score for each. It also automatically finds papers your results cite (and papers that cite them) so you don't miss foundational or follow-up work.

**Why it's better:** The existing plugins return 15 papers but only ~4 are on-topic — the rest is noise from biomedical sources like PubMed (papers that happen to mention "feedback" in a clinical context) or bioRxiv (neuroscience preprints sharing a keyword). research-mcp filters those out before ranking, so ~8 of 15 papers are actually useful. That's **53% precision vs 27% (academix) and 16% (paper-search)**.

**Token savings:** It replaces 65 tools from two plugins with 8 tools. The agent spends ~400 tokens browsing tools instead of ~12,000 — most of its context budget goes to research instead of navigating tool menus. This design is grounded in research showing tool catalog size inversely correlates with selection accuracy (Wang et al. 2026) and context window grows linearly with tool count (Dunkel 2026).

Benchmarked across **30 runs (10 queries × 3 MCPs)** against standalone `academix` and `paper-search-mcp`.

## Benchmark: V4 (10 Queries, 30 Runs — 2026-05-31)

| Metric | **research-mcp** (8 tools) | academix (8 tools) | paper-search (57 tools) |
|--------|---------------------------|-------------------|------------------------|
| **Avg relevant/query** | **7.9** | 4.0 | 6.4 |
| **Precision** | **52.7%** | 26.7% | 15.8% |
| **Wins (10 queries)** | **7** | 2 | 1 |
| **relevance_score** | **0–10 per paper** | — | — |
| **Source precision weights** | **Yes** | — | — |
| **Citation-weighted ranking** | **Yes** | Native (OpenAlex sort) | — |
| **Citation walk** | **Forward + backward** | Forward only | — |
| **Multi-factor ranking** | **Yes** | Citation × keyword only | Flat list |
| **Noise filtering** | **Yes** | None | None |
| **Errors** | 0 | 0 | **100%** (Zenodo crashes every query) |

**What research-mcp adds to academix + paper-search:**

research-mcp wraps the same underlying source libraries as academix (OpenAlex) and paper-search (21 academic sources). The wrapping is not the contribution. The contribution is what happens **after** the raw results come back:

1. **relevance_score per paper** — title term overlap + citation boost (1–3 points for 50+/100+/500+ cites). Neither academix nor paper-search provides this.
2. **Multi-factor ranking** — source_hits → relevance_score → abstract → citation_count → year. academix sorts by OpenAlex's proprietary relevance×citation blend. paper-search returns a flat list.
3. **Source precision weighting** — openaire+2, scopus+2, springer+2, academix+1, arxiv+1, semantic+1. High-precision sources rank higher automatically. Neither academix nor paper-search does this.
4. **Backward citation walk** — walks references (what papers cite) in addition to forward (who cites them). academix and paper-search only walk forward.
5. **Walk most-cited papers** — citation walk targets highly-cited papers, not just top-ranked. Surfaces seminal works through their references.
6. **relevance_score ≥3 filter** — removes papers with zero query term match and <50 citations. Zero false negatives across 150 benchmarked papers. Neither academix nor paper-search has any filter.
7. **Noise source exclusion** — bioRxiv (0% precision), medRxiv (0%), PubMed (~30%), Europe PMC (~17%), Zenodo (crashes) excluded by default. paper-search includes all 21 sources equally.

The raw search pipeline is the same library calls. The output is ranked, scored, and filtered.

### Per-Query Wins

research-mcp wins **7 of 10 benchmark queries** across diverse academic search tasks (precision-focused, recall-focused, and mixed-domain queries).

**Verdict:** research-mcp wins 7/10 queries with 52.7% precision — nearly 2× academix and 3× paper-search. The `relevance_score` field (0–10 per paper) lets you filter below score 3 to eliminate all pure-noise papers with zero false negatives.

## Why Precision Matters

Most academic MCPs return everything they find. research-mcp **ranks and filters**:

| Feature | What It Does | Impact |
|---------|-------------|--------|
| **relevance_score** | Title term overlap + citation boost (50+/100+/500+) | 0–10 per paper, bimodal distribution at 3 (weak) and 6 (moderate) |
| **Source precision weighting** | openaire+2, scopus+2, springer+2 | High-precision sources rank above noisy ones |
| **Citation-weighted ranking** | citation_count above year | Seminal papers (Zhang & Hyland 2018) beat recent preprints |
| **Forward + backward citation walk** | Who cites this + what it cites | Finds both follow-on work and foundations |
| **Walk most-cited papers** | Not top-ranked — most-cited get walked | Ellis 2005, Kormos 2012 surface through references |
| **relevance_score ≥3 filter** | Removes papers with zero term match + <50 citations | Zero false negatives in 150-paper benchmark |
| **No noisy sources** | Excludes bioRxiv, medRxiv, PubMed, Europe PMC, Zenodo | Eliminates 0%-precision biomedical noise |

### Selected Sources

| Source | Type | Notes |
|--------|------|-------|
| **OpenAlex** | Bibliographic (270M+) | Queried via academix with citation-weighted sort |
| **arXiv** | Preprints | Recent CS, ML, education preprints |
| **Semantic Scholar** | Academic search | Recent papers with citation data |
| **OpenAIRE** | EU open science | High-precision OA research across European repositories |
| **CrossRef** | DOI resolution | Broad metadata coverage |
| **Unpaywall** | OA PDF resolver | Enables open access full-text links |
| **Scopus** (conditional) | 26K+ journals | Requires Elsevier API key |
| **Springer** (conditional) | 29M+ papers | Open Access API (Metadata API key expired) |

### Noisy Sources (Excluded by Default for Precision-Optimised Search)

These sources are excluded from `search_literature`'s default source list because they returned near-zero precision in our domain. They may be valuable for other research domains (biomedical, clinical, neuroscience); enable them via `search_specific_sources` if needed.

| Source | Precision (our benchmark) | Why Excluded by Default |
|--------|--------------------------|------------------------|
| bioRxiv | **0%** | Neuroscience preprints — never matched our queries |
| medRxiv | **0%** | Epidemiology preprints — never matched our queries |
| PubMed | ~30% | Biomedical only — useful for medical queries, not general academic search |
| Europe PMC | ~17% | Biomedical noise for non-medical queries |
| Zenodo | **crashes** | `'str' object has no attribute 'isoformat'` on every query |
| Core | ~11% | Proceedings — low relevance outside CS/engineering |

## 8 Tools

| # | Tool | Purpose |
|---|------|---------|
| 1 | `search_literature` | 8 sources, dedup, auto citation walk, relevance scoring |
| 2 | `paper_lookup` | DOI/arXiv/title → metadata (auto-detect) |
| 3 | `walk_citations` | Multi-hop citation chain (S2 + OpenAlex) |
| 4 | `author_literature` | Search by author |
| 5 | `export_references` | RIS/CSV/JSON/BibTeX export |
| 6 | `read_paper` | Full text + Sci-Hub fallback |
| 7 | `extract_sections` | Selective reading (~80% token savings) |
| 8 | `compare_papers` | Side-by-side comparison |

## 8 Tools vs 73 Tools From 2 MCPs

research-mcp replaces **two separate MCP servers** (academix: 8 tools + paper-search: 57 tools + paper-distill: 8 tools) with **8 tools**. The tool surface drops from ~12,000 tokens to ~400 tokens.

| research-mcp (8 tools) | Replaces from academix | Replaces from paper-search | What's added |
|---|---|---|---|
| **search_literature** | `academic_search_papers` | `search_papers` + all 21 per-source search tools | Relevance scoring, precision weighting, citation walk, noise filtering, dedup |
| **paper_lookup** | `academic_get_paper_details` | `search_unpaywall` | Auto-detects DOI/arXiv/title, cross-source |
| **walk_citations** | `academic_get_citations` + `academic_get_citation_network` + `academic_get_related_papers` | — | Forward + backward, multi-hop |
| **author_literature** | `academic_search_author` | — | — |
| **export_references** | `academic_get_bibtex` | — | RIS/CSV/JSON/BibTeX |
| **read_paper** | — | 19 per-source download tools | Sci-Hub fallback |
| **extract_sections** | — | — | Selects only needed sections (~80% token savings) |
| **compare_papers** | — | — | Side-by-side across method/finding/limitation |

**Replaced entirely (no longer needed):**

| Replaced tools | Count |
|---------------|-------|
| Per-source search tools (search_arxiv, search_pubmed, ...) | 21 |
| Per-source download tools (download_arxiv, download_bibmix, ...) | 19 |
| Per-source read tools (read_arxiv, read_pubmed, ...) | 18 |
| paper-distill tools | 8 |
| academix cache/utility tools | 3 |
| **Total eliminated** | **69** |

**Tool surface:** ~400 tokens (vs ~12,000 for 2 separate MCPs)

## 8 Sources

| Source | Type | Key Required? |
|--------|------|---------------|
| arXiv | Preprints | No |
| Semantic Scholar | Academic search | Recommended |
| OpenAlex | 270M+ publications | No |
| CrossRef | DOI resolution | No |
| Unpaywall | OA PDF resolver | Email recommended |
| **OpenAIRE** | **EU open science** | **No** |
| Scopus | 26K+ journals | Elsevier API key |
| Springer Nature | 29M+ papers | Springer API key |

## Key Features

- **relevance_score per paper** — 0–10 scale, term overlap + citation boost. Filter below 3 to remove noise with zero false negatives
- **Source precision weighting** — high-precision sources (OpenAIRE, Scopus) rank higher automatically
- **Citation-weighted ranking** — citation_count above year, so seminal papers surface
- **Forward + backward citation walk** — walks most-cited papers, not top-ranked. Finds both foundations and follow-ons
- **No year filter by default** — includes seminal papers (1990-2017), not just recent
- **Auto dedup** — papers from multiple sources merged automatically
- **Noisy source exclusion** — bioRxiv, medRxiv, PubMed, Europe PMC excluded by default (benchmark-proven 0% precision)

## Setup

```bash
git clone https://github.com/chessy795/research-mcp.git
cd research-mcp
pip install -e .
```

### opencode Config

```json
{
  "mcp": {
    "research": {
      "type": "local",
      "command": ["python", "research_bundle.py"],
      "env": {
        "UNPAYWALL_EMAIL": "your@email.com",
        "SEMANTIC_SCHOLAR_API_KEY": "s2k-...",
        "ELSEVIER_API_KEY": "...",
        "SPRINGER_API_KEY": "..."
      }
    }
  }
}
```

### API Keys

| Key | Source | Status | What It Enables |
|-----|--------|--------|----------------|
| Semantic Scholar | [api.semanticscholar.org](https://api.semanticscholar.org/) | ✅ Required | 10 req/sec (vs 1/sec shared) |
| Unpaywall | Your institutional email | ✅ Recommended | OA PDF resolution |
| Elsevier/Scopus | [dev.elsevier.com](https://dev.elsevier.com/) | ✅ Live | Scopus search (26K+ journals) |
| Springer Nature (OA) | [dev.springernature.com](https://dev.springernature.com/) | ⚠️ OA only | OA search + PDF (Metadata key expired) |

**Note on Springer:** The Springer Metadata API key has expired. `search_springer` falls back to the Open Access API, which returns fewer (but freely accessible) results. Open Access PDF resolution still works.

## Usage

```python
# Search (8 sources, auto cite-walk, relevance scored)
search_literature(query="LLM feedback accuracy L2 writing", max_results=15)

# Lookup by DOI or title
paper_lookup(query="10.1016/j.asw.2018.02.004")

# Read specific sections (saves ~80% tokens)
extract_sections(paper_id="10.1016/j.asw.2018.02.004", sections=["abstract", "methods"])

# Export (RIS/CSV/JSON/BibTeX)
export_references(papers=[...], format="ris")

# Walk citations (Semantic Scholar + OpenAlex, forward + backward)
walk_citations(paper_id="10.1016/j.asw.2018.02.004", direction="forward", depth=2)
```

## Research Basis

- [Wang et al. 2026](https://arxiv.org/abs/2602.18914) — MCP description quality (+260% selection)
- [Dunkel 2026](https://arxiv.org/abs/2605.05247) — DADL: context window grows linearly with tool catalog
- [Hou et al. 2026](https://arxiv.org/abs/2504.14947) — MCP security landscape (16 threat scenarios)
- [Gan & Sun 2025](https://arxiv.org/abs/2505.03275) — RAG-MCP: tool routing for agent systems

## License

MIT
