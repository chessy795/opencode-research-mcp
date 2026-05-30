# research-mcp

A bundled MCP server that unifies academic research tools into one compact tool surface. Combines 21+ academic sources, citation graph walking, full-text PDF extraction, and smart query expansion into a single MCP server designed for LLM agents.

## Why Bundle?

Research from [Wang et al. 2026](https://arxiv.org/abs/2602.18914) shows that MCP description quality directly affects LLM tool selection by +260% probability. Bundling related tools:

- **Reduces token overhead** — one server vs three separate tool advertisements
- **Improves tool selection** — non-repeated, clearly named tools
- **Enables cross-source dedup** — papers found by multiple sources rank higher
- **Supports citation walking** — auto-follow citation graphs for deeper discovery

## Features

| Tool | What it does |
|---|---|
| `search_literature` | Search 21+ sources with query expansion + auto-citation walk |
| `paper_lookup` | Paper details by DOI, arXiv, OpenAlex, Semantic Scholar |
| `citation_intelligence` | Citation graph: citing papers, references, network |
| `walk_citations` | Multi-hop citation chain walker |
| `author_literature` | Papers by author |
| `export_bibliography` | BibTeX export |
| `search_specific_sources` | Direct source control (21 sources) |
| `read_paper` | Full text download + PDF extraction |
| `batch_read` | Concurrent full text for multiple papers |
| `curate_research` | Ranking, dedup, review prompts |
| `paper_distill_pipeline` | Paper Distill workflow access |

## Sources (21+)

arxiv, semantic scholar, openalex, crossref, dblp, pubmed, pmc, europepmc, core, openaire, doaj, base, hal, zenodo, ssrn, google scholar, biorxiv, medrxiv, iacr, citeseerx, unpaywall

## Setup

### Prerequisites

Install the three upstream MCP servers:

```bash
uv tool install academix
uv tool install paper-search-mcp
uv tool install paper-distill-mcp
```

### opencode Configuration

Add to your `opencode.json`:

```json
{
  "mcp": {
    "research": {
      "type": "local",
      "command": ["python", "path/to/research_bundle.py"],
      "env": {
        "ACADEMIX_EMAIL": "your@email.com",
        "UNPAYWALL_EMAIL": "your@email.com",
        "SEMANTIC_SCHOLAR_API_KEY": "optional"
      },
      "enabled": true
    }
  }
}
```

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ACADEMIX_EMAIL` | Recommended | Email for API rate limits |
| `UNPAYWALL_EMAIL` | Recommended | Email for Unpaywall OA resolution (use institutional email for better access) |
| `SEMANTIC_SCHOLAR_API_KEY` | Optional | Higher rate limits for Semantic Scholar |

## Usage

The model calls these tools automatically. Key parameters for `search_literature`:

- `profile`: `"fast"` (8 best sources) or `"broad"` (all 21 sources)
- `expand_queries`: Auto-expand acronyms (LLM -> large language model)
- `auto_cite_walk`: Auto-walk citation graph for top results
- `year_from`/`year_to`: Year range filter

## Architecture

```
research_bundle.py
  |
  +-- academix (metadata, citations, BibTeX, related papers)
  +-- paper-search-mcp (21+ sources, PDF download, text extraction)
  +-- paper-distill-mcp (curation, ranking, digest, push)
```

## Research Basis

This bundle design is informed by:

- **Wang et al. 2026** - "From Docs to Descriptions" - MCP description quality study (10,831 servers)
- **Dunkel 2026** - DADL - Progressive tool disclosure for token efficiency
- **Schlapbach 2026** - SGD-MCP convergence patterns
- **Hou et al. 2026** - "MCP Landscape" - Security threats and future directions

## License

MIT
