# research-mcp

A bundled MCP server that unifies 8 academic sources into 8 curated tools for LLM research agents. Benchmarked across 30 runs (10 queries × 3 MCPs) against standalone `academix` and `paper-search-mcp`.

## Benchmark (30 Runs, 10 Queries)

| Metric | research-mcp (8 tools) | academix (8 tools) | paper-search (57 tools) |
|--------|----------------------|-------------------|------------------------|
| **Avg relevant/query** | **10.7** | 6.4 | 14.8 |
| **Precision** | **71%** | 43% | 38% |
| **Wins (10 queries)** | **5** (4 outright + 1 tie) | 0 | 5 |
| **Abstract coverage** | **100%** | 90% | 80% |
| **Token efficiency (standard)** | 0.71 rel/1K tok | **1.28 rel/1K tok** | 0.30 |
| **Token efficiency (compact)** | **1.4 rel/1K tok** | 1.28 rel/1K tok | 0.30 |
| **Errors** | 0% | 0% | 70% of queries |

**Verdict:** research-mcp wins 5/10 queries with highest precision (71%). With `compact=True`, it also beats academix on token efficiency (1.4 vs 1.28). Paper-search wins on raw volume but carries a 57-tool surface and 70% error rate.

## 8 Tools

| # | Tool | Purpose |
|---|------|---------|
| 1 | `search_literature` | 8 sources, dedup, auto citation walk, **compact mode** |
| 2 | `paper_lookup` | DOI/arXiv/title → metadata (auto-detect) |
| 3 | `walk_citations` | Multi-hop citation chain (S2 + OpenAlex) |
| 4 | `author_literature` | Search by author |
| 5 | `export_references` | RIS/CSV/JSON/BibTeX export |
| 6 | `read_paper` | Full text + Sci-Hub fallback |
| 7 | `extract_sections` | Selective reading (~80% token savings) |
| 8 | `compare_papers` | Side-by-side comparison |

**Tool surface:** ~400 tokens (vs ~12,000 for 3 separate MCPs)

## 8 Sources

| Source | Type | Key Required? |
|--------|------|---------------|
| arXiv | Preprints | No |
| Semantic Scholar | Academic search | Recommended |
| OpenAlex | 270M+ publications | No |
| CrossRef | DOI resolution | No |
| PubMed | Biomedical | No |
| Unpaywall | OA PDF resolver | Email recommended |
| Scopus | 26K+ journals | Elsevier API key |
| Springer Nature | 29M+ papers | Springer API key |

## Key Features

- **Compact mode** — `search_literature(compact=True)` strips abstracts, saves ~50% tokens
- **OpenAlex citation walk** — free, no rate limits (unlike Semantic Scholar)
- **No year filter by default** — includes seminal papers (1990-2017), not just recent
- **Auto dedup** — papers from multiple sources merged automatically
- **Source filtering** — excludes noisy sources (bioRxiv, medRxiv) by default

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

| Key | Source | What It Enables |
|-----|--------|----------------|
| Semantic Scholar | [api.semanticscholar.org](https://api.semanticscholar.org/) | 10 req/sec (vs 1/sec shared) |
| Unpaywall | Your institutional email | OA PDF resolution |
| Elsevier/Scopus | [dev.elsevier.com](https://dev.elsevier.com/) | Scopus search (26K+ journals) |
| Springer Nature | [dev.springernature.com](https://dev.springernature.com/) | Springer search + OA PDF |

## Usage

```python
# Search (8 sources, auto cite-walk). Use compact=True to save tokens.
search_literature(query="online writing L2 research", max_results=15)
search_literature(query="online writing L2 research", compact=True)

# Lookup by DOI or title
paper_lookup(query="10.1016/j.asw.2018.02.004")

# Read specific sections (saves ~80% tokens)
extract_sections(paper_id="10.1016/j.asw.2018.02.004", sections=["abstract", "methods"])

# Export (RIS/CSV/JSON/BibTeX)
export_references(papers=[...], format="ris")

# Walk citations (uses both Semantic Scholar + OpenAlex)
walk_citations(paper_id="10.1016/j.asw.2018.02.004", direction="forward", depth=2)
```

## Research Basis

- [Wang et al. 2026](https://arxiv.org/abs/2602.18914) — MCP description quality (+260% selection)
- [Dunkel 2026](https://arxiv.org/abs/2605.05247) — DADL: context window grows linearly with tool catalog
- [Hou et al. 2026](https://arxiv.org/abs/2504.14947) — MCP security landscape (16 threat scenarios)
- [Gan & Sun 2025](https://arxiv.org/abs/2505.03275) — RAG-MCP: tool routing for agent systems

## License

MIT
