# research-mcp

A bundled MCP server that unifies academic research tools into one compact tool surface. Combines 6 major academic sources (with access to 21+ via source-specific tools), citation graph walking, full-text PDF extraction, Sci-Hub availability checking, and smart query expansion into a single MCP server designed for LLM agents.

## The Problem

LLMs waste tokens and make poor tool selections when drowning in redundant MCP tools. Research from [Wang et al. 2026](https://arxiv.org/abs/2602.18914) (10,831 MCP servers studied) found:

- **73% of MCP servers** have repeated tool names across servers
- **+260% selection probability** when tool descriptions are clear and non-redundant
- **Linear context growth** — each additional MCP server adds ~3,000-5,000 tokens of tool descriptions to the context window

The three upstream academic MCPs (`academix`, `paper-search-mcp`, `paper-distill-mcp`) provide overlapping search capabilities across 43+ combined tools. This bundle merges them into **13 curated tools** — a 70% reduction in tool surface area while preserving full capability.

## What This Bundle Does

| Tool | What It Does | When to Use |
|---|---|---|
| `search_literature` | Federated search across 6 major sources with dedup, query expansion, auto-citation walking | First step — find papers |
| `paper_lookup` | Paper details by DOI, arXiv ID, OpenAlex ID, or Semantic Scholar ID | Need full metadata for a specific paper |
| `citation_intelligence` | Citing papers, references, related work, or full citation network graph | Understand citation context |
| `walk_citations` | Multi-hop citation chain walker (follow citation graphs N hops deep) | Find related work through citations |
| `author_literature` | Find all papers by a specific author with year filters | Author-based search |
| `export_bibliography` | BibTeX export with LaTeX-aware escaping and DBLP native lookup | Build bibliography |
| `search_specific_sources` | Direct source control — pick exactly which databases to query (Scopus, Springer, etc.) | Need specific sources |
| `search_scihub` | Dedicated Sci-Hub download by DOI, title, PMID, or URL | Need a specific paper |
| `read_paper` | Full-text PDF download + text extraction from 12+ open-access sources | Need complete document |
| `extract_sections` | **PRIMARY READING TOOL** — pull specific sections from full text (~80% token savings) | Read papers selectively |
| `compare_papers` | Side-by-side comparison across multiple papers | Compare methods/findings |
| `curate_research` | Paper ranking, dedup filtering, review prompt generation | Curate paper collections |
| `paper_distill_pipeline` | Session management, topic preferences, Zotero collection, push digests | Manage recurring workflows |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  research_bundle.py                      │
│             Single FastMCP server process                 │
├─────────────┬──────────────┬────────────┬───────────────┤
│  Academix   │ Paper Search │  Paper     │  Publisher    │
│  metadata   │ 6 default    │  Distill   │  APIs         │
│  citations  │ PDF download │  curation  │  Scopus       │
│  BibTeX     │ text extract │  ranking   │  Springer     │
│  networks   │ Sci-Hub/OA   │  digests   │  Sci-Hub      │
│             │              │  Zotero    │               │
└─────────────┴──────────────┴────────────┴───────────────┘
```

## Agent Workflow

### The Core Insight

**More tokens ≠ better reasoning.** LLMs have an "attention sweet spot" — beyond ~30K tokens of raw text, synthesis quality drops. The best-case scenario isn't "dump everything in context." It's **multi-pass selective extraction**.

The MCP is the **retrieval layer**. The agent is the **reasoning layer**. The boundary is clear.

### The Multi-Pass Pattern

```
Pass 1: CLASSIFY what you need
  Agent: "I need to compare methods across 5 papers"
  → Action: extract_sections with sections=["methods", "findings"]

Pass 2: SELECTIVE extraction (NOT full text)
  paper_1 → extract_sections(["methods","findings"])  → ~3,000 tokens
  paper_2 → extract_sections(["methods","findings"])  → ~3,000 tokens
  paper_3 → extract_sections(["methods","findings"])  → ~3,000 tokens
                              TOTAL: ~9,000 tokens (not 45,000)

Pass 3: BUILD a synthesis matrix before writing
  ┌──────────┬────────────────────┬─────────────────────┬──────────────────┐
  │ Paper    │ Method             │ Key Finding         │ Limitation       │
  ├──────────┼────────────────────┼─────────────────────┼──────────────────┤
  │ Wang 24  │ RAG-MCP            │ 50% token reduction │ CS-only testing  │
  │ Liu 23   │ Citation walking   │ +15% recall         │ 8s per query     │
  │ Chen 24  │ Multi-source fusion│ +22% precision      │ Needs 3+ keys    │
  └──────────┴────────────────────┴─────────────────────┴──────────────────┘

Pass 4: WRITE from the matrix, not from the papers
  Matrix = ~2K tokens. Raw papers = ~45K tokens.
  The matrix IS the distilled knowledge.
```

### What the MCP Should (and Shouldn't) Do

| MCP (Tool Layer) | Agent (Reasoning Layer) |
|---|---|
| Search for papers | Decide what to search for |
| Extract specific sections | Synthesize findings across papers |
| Compare papers side-by-side | Build the synthesis matrix |
| Download full text PDF | Write the final report |
| Walk citation graphs | Interpret citation patterns |

**The MCP should NOT:** summarize papers, compare findings analytically, write synthesis, or check venue quality. Those are the agent's job.

### Agent Best Practices

1. **Never read full text unless you have to** — use `extract_sections` first
2. **Build a matrix before writing** — structured comparison beats free-form reading
3. **One paper at a time** — sequential extraction with matrix building, not parallel full-text dumps
4. **Synthesize from the matrix, not from the papers** — the matrix is the distilled knowledge

### Token Budget Example

| Approach | Tokens Consumed | Synthesis Quality |
|---|---|---|
| Dump all 10 full papers | ~80,000 | Poor (attention decay) |
| extract_sections per paper | ~12,000 | High (focused extraction) |
| Matrix + synthesis | ~2,000 | High (distilled knowledge) |

### Full Workflow Example

```
Agent: "Compare RAG-based vs citation-based approaches for literature discovery"

MCP: search_literature(query="RAG literature discovery", max_results=10)
     → Returns 10 papers with metadata + abstracts

MCP: extract_sections(papers[0].arxiv_id, sections=["methods","findings"])
     → Returns ~3K tokens of relevant sections only

MCP: extract_sections(papers[1].arxiv_id, sections=["methods","findings"])
     → Returns ~3K tokens of relevant sections

Agent: [builds synthesis matrix from extracted sections]

Agent: [writes comparison from matrix, not from raw text]

Total tokens: ~12K (matrix + extracted sections)
vs dumping all 10 full papers: ~80K (attention decay kicks in)
```

## Sources

The default `search_literature` tool queries 6 high-impact sources that cover 95%+ of academic literature:

| Category | Sources |
|---|---|
| **Preprints** | arXiv |
| **Academic search** | Semantic Scholar, OpenAlex, CrossRef |
| **Biomedical** | PubMed |
| **Open access** | Unpaywall |

For niche sources, use `search_specific_sources` — it can query any of these additional backends: **Scopus** (Elsevier), **Springer Nature**, bioRxiv, medRxiv, IACR ePrint, DBLP, PMC, EuropePMC, CORE, OpenAIRE, DOAJ, BASE, HAL, Zenodo, SSRN, CiteSeerX.

## Features

### Smart Search
- **6 best sources**: arXiv, Semantic Scholar, OpenAlex, CrossRef, PubMed, Unpaywall — covers 95%+ of relevant results
- **Query expansion**: Automatically expands acronyms (`LLM` → `large language model`, `RAG` → `retrieval augmented generation`)
- **Cross-source deduplication**: Papers found by multiple sources are ranked higher
- **Auto-citation walking**: Automatically follows citation graphs for top results
- **Sci-Hub availability**: Optional `check_scihub=True` adds per-paper availability flag

### Full-Text Access
- **Selective reading**: `extract_sections` pulls only the sections you need (~80% token savings)
- **12 source-specific readers**: arXiv, Semantic Scholar, bioRxiv, medRxiv, IACR, OpenAIRE, CiteSeerX, DOAJ, BASE, Zenodo, HAL, Sci-Hub
- **Smart fallback cascade**: Tries source-native → OA repositories → Unpaywall → Sci-Hub (optional)
- **Springer OA fallback**: DOI→PDF via Springer Open Access API
- **PDF text extraction**: Uses `pypdf` for page-by-page extraction

### Citation Analysis
- **Citation intelligence**: Citing papers, references, related work in one call
- **Multi-hop walking**: Follow citation chains N hops deep (forward, backward, or both)
- **Network graphs**: JSON graph format with nodes and edges for visualization

### Curation Pipeline
- **Paper ranking**: 4-factor scoring (relevance, recency, impact, novelty)
- **Topic management**: Configure research interests with weighted keywords
- **Push digests**: Send daily/weekly digests to Telegram, Discord, Feishu, or WeCom
- **Zotero integration**: Collect papers with full metadata enrichment

## Installation

### Option 1: uv tool install (recommended)

```bash
# Install the three upstream servers
uv tool install academix
uv tool install paper-search-mcp
uv tool install paper-distill-mcp

# Clone and install this bundle
git clone https://github.com/chessy795/research-mcp.git
cd research-mcp
pip install -e .
```

### Option 2: Direct download

```bash
# Install dependencies
pip install academix paper-search-mcp paper-distill-mcp fastmcp

# Download research_bundle.py and place it anywhere
```

## Configuration

### opencode

Add to your `opencode.json`:

```json
{
  "mcp": {
    "research": {
      "type": "local",
      "command": ["python", "/path/to/research_bundle.py"],
      "env": {
        "UNPAYWALL_EMAIL": "your@email.com",
        "SEMANTIC_SCHOLAR_API_KEY": "optional",
        "ELSEVIER_API_KEY": "optional",
        "SPRINGER_API_KEY": "optional"
      },
      "enabled": true
    }
  }
}
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "research": {
      "command": "python",
      "args": ["/path/to/research_bundle.py"],
      "env": {
        "UNPAYWALL_EMAIL": "your@email.com"
      }
    }
  }
}
```

## API Key Setup

All API keys are optional but recommended for better rate limits and access.

### Semantic Scholar (Recommended)

1. Go to [api.semanticscholar.org/api-docs/GraphQl](https://api.semanticscholar.org/api-docs/GraphQl)
2. Click **"Get API Key"** in the top right
3. Sign up with your email
4. Copy your API key (format: `s2k-xxxxxxxxxxxx`)
5. Set as `SEMANTIC_SCHOLAR_API_KEY` environment variable

**Rate limits:** 1 req/sec (no key) → 10 req/sec (with key)

### Unpaywall (Recommended)

1. Go to [unpaywall.org/products/api](https://unpaywall.org/products/api)
2. Enter your email address
3. No signup required — just use your institutional email
4. Set as `UNPAYWALL_EMAIL` environment variable

**Why:** Unpaywall resolves DOIs to open-access PDF URLs. Using your institutional email may unlock additional OA copies.

### Elsevier / Scopus (Optional)

1. Go to [dev.elsevier.com](https://dev.elsevier.com/)
2. Click **"Create API Key"**
3. Fill in your details (name, email, institution)
4. Select **"Scopus"** as the product
5. Copy your API key (format: `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`)
6. Set as `ELSEVIER_API_KEY` environment variable

**What it enables:** Scopus search (26,000+ journals, 18M+ papers) via `search_specific_sources`

### Springer Nature (Optional)

1. Go to [dev.springernature.com](https://dev.springernature.com/)
2. Click **"Register for an API Key"**
3. Fill in your details
4. Select **"Meta API"** and **"Open Access API"**
5. Copy your API key (format: `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`)
6. Set as `SPRINGER_API_KEY` environment variable

**What it enables:** Springer Nature search + Open Access PDF resolution (29M+ papers)

### All Environment Variables

| Variable | Required | Description |
|---|---|---|
| `UNPAYWALL_EMAIL` | Recommended | Email for Unpaywall OA resolution. Use your institutional email for better access to paywalled papers. |
| `SEMANTIC_SCHOLAR_API_KEY` | Recommended | Higher rate limits for Semantic Scholar (10 req/sec vs 1 req/sec) |
| `ELSEVIER_API_KEY` | Optional | Enables Scopus search via Elsevier API |
| `SPRINGER_API_KEY` | Optional | Enables Springer Nature search and Open Access PDF resolution |
| `ACADEMIX_EMAIL` | Optional | Email for Academix caching layer |

## Usage Examples

### Basic literature search

The model will call `search_literature` automatically. Key parameters:

```
query: "retrieval augmented generation"
expand_queries: true      # auto-expand "RAG" → "retrieval augmented generation"
auto_cite_walk: true      # follow citation graph for top 3 results
year_from: 2020           # only papers from 2020 onwards
check_scihub: true        # add Sci-Hub availability to results
```

### Read specific sections (recommended)

```
extract_sections(paper_id="2305.14283", sections=["methods", "findings"])
```

### Read full paper (only when needed)

```
read_paper(paper_id="2305.14283")
read_paper(paper_id="10.1038/s41586-020-2649-2", use_scihub=True)
```

### Compare papers

```
compare_papers(
    papers=[{arxiv_id: "2305.14283"}, {doi: "10.1038/s41586-020-2649-2"}],
    aspects=["method", "finding", "limitation"]
)
```

### Walk citation chains

```
walk_citations(
    paper_id="2305.14283",
    direction="forward",
    depth=2,
    max_papers_per_hop=10
)
```

### Export BibTeX

```
export_bibliography(paper_ids=["2305.14283", "10.1038/s41586-020-2649-2"])
```

### Search Scopus or Springer

```
search_specific_sources(
    query="deep learning medical imaging",
    sources="scopus,springer",
    max_results_per_source=10
)
```

### Download via Sci-Hub

```
search_scihub(identifier="10.1038/s41586-020-2649-2")
```

## Tool Comparison

| Capability | Separate MCPs (3 servers) | This Bundle (1 server) |
|---|---|---|
| Tool count | 43+ tools | 13 tools |
| Context tokens | ~15,000 | ~4,500 |
| Search sources | 21+ | 21+ (same) |
| Cross-source dedup | Manual | Automatic |
| Citation walking | Manual (separate calls) | Auto (built into search) |
| Query expansion | None | Built-in |
| Selective reading | None | extract_sections (~80% savings) |
| Sci-Hub availability | None | Per-paper check |
| Process count | 3 | 1 |
| Tool selection accuracy | Lower (redundant names) | Higher (unique names) |

## Performance

Benchmarked on a representative search query:

| Metric | Result |
|---|---|
| Search latency | ~16s (6 sources in parallel) |
| Papers returned | 20 (deduplicated from ~80 raw) |
| Top result citations | 643 |
| Context tokens | ~4,500 (vs ~15,000 for separate MCPs) |
| Tool surface | 13 (vs 43+ for separate MCPs) |

## Research Basis

This bundle design is informed by:

- **Wang et al. 2026** — ["From Docs to Descriptions"](https://arxiv.org/abs/2602.18914) — MCP description quality study of 10,831 servers. Found that 73% have repeated tool names and clear descriptions give +260% selection probability.
- **Dunkel 2026** — [DADL](https://arxiv.org/abs/2605.05247) — Progressive tool disclosure for token efficiency. Demonstrated linear context growth with tool count.
- **Hou et al. 2026** — ["MCP Landscape"](https://arxiv.org/abs/2504.14947) — Security threats and future directions for MCP. Identified 16 threat scenarios across 4 categories.
- **Gan & Sun 2025** — [RAG-MCP](https://arxiv.org/abs/2505.03275) — Retrieval-augmented tool selection for MCP. Reduces prompt tokens by 50%+ and triples selection accuracy.

## Upstream Packages

| Package | What It Provides | License |
|---|---|---|
| [academix](https://pypi.org/project/academix/) | Metadata, citations, BibTeX, citation networks | MIT |
| [paper-search-mcp](https://pypi.org/project/paper-search-mcp/) | 21+ source search, PDF download, text extraction | MIT |
| [paper-distill-mcp](https://pypi.org/project/paper-distill-mcp/) | Curation, ranking, digests, Zotero integration | MIT |

## Contributing

Contributions welcome. The bundle is intentionally thin (~1200 lines) — it delegates to upstream packages. Changes should keep the tool surface compact.

## License

MIT
