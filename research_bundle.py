"""Bundled academic research MCP server.

Integrates Academix (metadata + citations), Paper Search (21+ sources + PDF/text),
and Paper Distill (curation pipeline) into one compact tool surface.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from publisher_apis import search_scopus, search_springer, springer_resolve_oa

TOOL_SITE_PACKAGES = [
    Path.home() / "AppData" / "Roaming" / "uv" / "tools" / "paper-distill-mcp" / "Lib" / "site-packages",
    Path.home() / "AppData" / "Roaming" / "uv" / "tools" / "paper-search-mcp" / "Lib" / "site-packages",
    Path.home() / "AppData" / "Roaming" / "uv" / "tools" / "academix" / "Lib" / "site-packages",
]

for site_packages in reversed(TOOL_SITE_PACKAGES):
    site_path = str(site_packages)
    if site_packages.exists() and site_path not in sys.path:
        sys.path.insert(0, site_path)

from fastmcp import FastMCP  # noqa: E402

from academix.aggregator import AcademicAggregator  # noqa: E402
from academix import server as academix_server  # noqa: E402
from paper_search_mcp import server as paper_search  # noqa: E402
from mcp_server import server as paper_distill  # noqa: E402

_academix: AcademicAggregator | None = None


@asynccontextmanager
async def lifespan(app: FastMCP) -> AsyncIterator[None]:
    global _academix
    _academix = AcademicAggregator(
        email=os.environ.get("ACADEMIX_EMAIL"),
        semantic_scholar_api_key=os.environ.get("SEMANTIC_SCHOLAR_API_KEY"),
    )
    academix_server._aggregator = _academix
    try:
        yield
    finally:
        if _academix is not None:
            await _academix.close()
        academix_server._aggregator = None
        _academix = None


mcp = FastMCP("research", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_load(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
    return value


def _authors(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [a.strip() for a in value.replace(";", ",").split(",") if a.strip()]
    out = []
    for a in value:
        if isinstance(a, dict):
            name = a.get("name") or a.get("author")
            if name:
                out.append(str(name))
        elif a:
            out.append(str(a))
    return out


def _norm_id(value: Any) -> str:
    return str(value or "").strip().lower().removeprefix("https://doi.org/").removeprefix("http://doi.org/")


def _paper_key(paper: dict[str, Any]) -> str:
    doi = _norm_id(paper.get("doi"))
    if doi:
        return f"doi:{doi}"
    arxiv = _norm_id(paper.get("arxiv_id") or paper.get("arxiv"))
    if arxiv:
        return f"arxiv:{arxiv}"
    pmid = str(paper.get("pmid") or paper.get("paper_id") or "").strip()
    if pmid.startswith("PMID:"):
        return pmid.lower()
    title = " ".join(str(paper.get("title") or "").lower().split())
    year = str(paper.get("year") or paper.get("published_date") or "")[:4]
    return f"title:{title[:160]}:{year}"


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _normalize_paper(paper: dict[str, Any], source: str) -> dict[str, Any]:
    p = dict(paper)
    raw = p.pop("raw", p)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k not in p or p[k] is None:
                p[k] = v
    authors = _authors(p.get("authors"))
    year_raw = p.get("year") or str(p.get("published_date") or "")[:4]
    return {
        "title": p.get("title"),
        "authors": authors,
        "year": _to_int(year_raw, 0) or None,
        "venue": p.get("venue") or p.get("journal") or p.get("publisher"),
        "doi": p.get("doi"),
        "arxiv_id": p.get("arxiv_id") or p.get("arxiv"),
        "pmid": p.get("pmid"),
        "paper_id": p.get("id") or p.get("paper_id"),
        "url": p.get("url"),
        "pdf_url": p.get("pdf_url") or p.get("open_access_url"),
        "abstract": p.get("abstract"),
        "citation_count": _to_int(
            p.get("citation_count") or p.get("citations") or p.get("cited_by_count"), 0
        ),
        "keywords": p.get("keywords") or p.get("categories"),
        "sources": sorted(set(s for s in [source, str(p.get("source") or source)] if s)),
    }


def _merge_papers(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in items:
        key = _paper_key(item)
        if not key or key.startswith("title::"):
            continue
        existing = merged.get(key)
        if existing is None:
            item["source_hits"] = len(set(item.get("sources") or []))
            merged[key] = item
            continue
        new_sources = set(existing.get("sources") or []) | set(item.get("sources") or [])
        existing["sources"] = sorted(new_sources)
        existing["source_hits"] = len(new_sources)
        for field in ("abstract", "doi", "arxiv_id", "pmid", "paper_id", "url", "pdf_url", "venue", "year", "keywords"):
            if not existing.get(field) and item.get(field):
                existing[field] = item[field]
        existing["citation_count"] = max(
            _to_int(existing.get("citation_count")), _to_int(item.get("citation_count"))
        )
    ranked = sorted(
        merged.values(),
        key=lambda p: (_to_int(p.get("source_hits")), _to_int(p.get("citation_count")), _to_int(p.get("year"))),
        reverse=True,
    )
    return ranked[:limit]


BEST_SOURCES = "arxiv,semantic,openalex,crossref,pubmed,unpaywall"


def _expand_query(query: str) -> list[str]:
    """Generate query variations for broader recall."""
    expansions = [query]
    words = query.lower().split()
    acronyms = {
        "llm": ["large language model", "language models"],
        "nlp": ["natural language processing"],
        "ml": ["machine learning"],
        "dl": ["deep learning"],
        "cv": ["computer vision"],
        "rl": ["reinforcement learning"],
        "ai": ["artificial intelligence"],
        "bert": ["bidirectional encoder representations"],
        "gpt": ["generative pre-trained transformer"],
        "rag": ["retrieval augmented generation"],
        "mcp": ["model context protocol"],
        "iot": ["internet of things"],
    }
    for word in words:
        if word in acronyms:
            for expansion in acronyms[word]:
                expansions.append(query.lower().replace(word, expansion))
    return expansions[:2]


def _detect_source_from_paper(paper: dict[str, Any]) -> str:
    """Auto-detect the best source for full-text retrieval from paper metadata."""
    arxiv_id = paper.get("arxiv_id") or paper.get("arxiv")
    if arxiv_id:
        return "arxiv"
    doi = str(paper.get("doi") or "").lower()
    if "biorxiv" in doi:
        return "biorxiv"
    if "medrxiv" in doi:
        return "medrxiv"
    pmid = str(paper.get("pmid") or "")
    if pmid:
        return "semantic"
    source_list = paper.get("sources") or []
    for s in source_list:
        if s in ("arxiv", "semantic", "biorxiv", "medrxiv", "iacr", "openaire",
                 "citeseerx", "doaj", "base", "zenodo", "hal", "pmc", "europepmc"):
            return s
    return ""


# ---------------------------------------------------------------------------
# Core tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_literature(
    query: str,
    max_results: int = 20,
    year_from: int | None = None,
    year_to: int | None = None,
    expand_queries: bool = True,
    auto_cite_walk: bool = True,
    cite_walk_depth: int = 1,
    cite_walk_max_papers: int = 3,
) -> dict[str, Any]:
    """Best default literature search across 6 major academic sources.

    Queries arXiv, Semantic Scholar, OpenAlex, CrossRef, PubMed, and Unpaywall
    in parallel, then deduplicates and ranks by cross-source hits + citation count.

    For niche sources (DBLP, bioRxiv, IEEE, etc.) use search_specific_sources.

    Args:
        query: Search query string
        max_results: Maximum results to return (default 20)
        year_from: Filter papers from this year
        year_to: Filter papers until this year
        expand_queries: Auto-expand acronyms (LLM->large language model)
        auto_cite_walk: Auto-walk citation graph for top results
        cite_walk_depth: Citation graph walk depth (1=direct citations only)
        cite_walk_max_papers: How many top papers to walk citations for
    """
    year = None
    if year_from and year_to:
        year = f"{year_from}-{year_to}"
    elif year_from:
        year = f"{year_from}-"
    elif year_to:
        year = f"-{year_to}"

    queries = _expand_query(query) if expand_queries else [query]

    tasks = []
    for q in queries:
        tasks.extend([
            academix_server.academic_search_papers(
                query=q,
                year_from=year_from,
                year_to=year_to,
                sort="relevance",
                limit=min(max(max_results, 1), 100),
                response_format="json",
            ),
            paper_search.search_papers(
                query=q,
                max_results_per_source=max(3, min(max_results, 30)),
                sources=BEST_SOURCES,
                year=year,
            ),
        ])

    try:
        outputs = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=45.0,
        )
    except asyncio.TimeoutError:
        outputs = [asyncio.TimeoutError("Search timed out after 45s")] * len(tasks)
    papers: list[dict[str, Any]] = []
    errors: dict[str, str] = {}

    for i, out in enumerate(outputs):
        q_idx = i // 2
        backend = i % 2
        q_label = queries[q_idx] if q_idx < len(queries) else query

        if isinstance(out, Exception):
            backend_name = ["academix", "paper_search"][backend]
            errors[f"{backend_name}_{q_label}"] = str(out)
            continue

        if backend == 0:
            data = _json_load(out)
            for paper in data.get("papers", []) if isinstance(data, dict) else []:
                papers.append(_normalize_paper(paper, "academix"))
        elif backend == 1:
            data = _json_load(out)
            for paper in data.get("papers", []) if isinstance(data, dict) else []:
                papers.append(_normalize_paper(paper, "paper-search"))

    merged = _merge_papers(papers, max_results)

    result = {
        "query": query,
        "queries_used": queries,
        "total_before_dedupe": len(papers),
        "returned": len(merged),
        "errors": errors,
        "papers": merged,
    }

    if auto_cite_walk and merged:
        walk_ids = []
        for p in merged[:cite_walk_max_papers]:
            pid = p.get("doi") or p.get("arxiv_id") or p.get("paper_id")
            if pid:
                walk_ids.append(pid)

        async def _fetch_citations(pid: str) -> list[dict[str, Any]]:
            try:
                citations_data = _json_load(
                    await academix_server.academic_get_citations(
                        pid, limit=5, response_format="json"
                    )
                )
                citing = citations_data.get("citing_papers", []) if isinstance(citations_data, dict) else []
                return [_normalize_paper(cp, "citation-walk") for cp in citing[:5]]
            except Exception:
                return []

        citation_results = await asyncio.gather(
            *[_fetch_citations(pid) for pid in walk_ids],
            return_exceptions=True,
        )
        citation_papers: list[dict[str, Any]] = []
        for r in citation_results:
            if isinstance(r, list):
                citation_papers.extend(r)

        if citation_papers:
            all_papers = merged + citation_papers
            result["papers"] = _merge_papers(all_papers, max_results + 10)
            result["citation_walk_found"] = len(citation_papers)

    return result


@mcp.tool()
async def paper_lookup(paper_id: str, include_related: bool = False) -> dict[str, Any]:
    """Get detailed metadata for a paper by DOI, arXiv ID, OpenAlex ID, Semantic Scholar ID, or title-like ID.

    Use after search_literature when you need full abstract, identifiers, citation count, PDF URL, or related work.
    """
    errors: dict[str, str] = {}
    result: dict[str, Any] = {"paper_id": paper_id}
    try:
        details = await academix_server.academic_get_paper_details(paper_id, response_format="json")
        result["details"] = _json_load(details)
    except Exception as exc:
        errors["academix_details"] = str(exc)

    if include_related:
        try:
            related = await academix_server.academic_get_related_papers(
                paper_id=paper_id, limit=10, response_format="json"
            )
            result["related"] = _json_load(related)
        except Exception as exc:
            errors["related"] = str(exc)

    if not result.get("details") or result.get("details") == {"raw": "No paper found"}:
        try:
            result["crossref"] = await paper_search.get_crossref_paper_by_doi(paper_id)
        except Exception as exc:
            errors["crossref"] = str(exc)

    result["errors"] = errors
    return result


@mcp.tool()
async def citation_intelligence(
    paper_id: str,
    mode: Literal["citing", "references", "related", "network", "all"] = "all",
    limit: int = 20,
) -> dict[str, Any]:
    """Explore citation context for a paper: citing papers, references, related work, or network graph."""
    result: dict[str, Any] = {"paper_id": paper_id, "mode": mode, "errors": {}}
    if mode in ("citing", "all"):
        try:
            result["citing"] = _json_load(
                await academix_server.academic_get_citations(paper_id, limit=limit, response_format="json")
            )
        except Exception as exc:
            result["errors"]["citing"] = str(exc)
    if mode in ("related", "all"):
        try:
            result["related"] = _json_load(
                await academix_server.academic_get_related_papers(paper_id, limit=limit, response_format="json")
            )
        except Exception as exc:
            result["errors"]["related"] = str(exc)
    if mode in ("references", "network", "all"):
        direction = "cited" if mode == "references" else "both"
        try:
            result["network"] = _json_load(
                await academix_server.academic_get_citation_network(
                    paper_id, direction=direction, max_nodes=max(10, min(200, limit * 2))
                )
            )
        except Exception as exc:
            result["errors"]["network"] = str(exc)
    return result


@mcp.tool()
async def walk_citations(
    paper_id: str,
    direction: Literal["forward", "backward", "both"] = "forward",
    depth: int = 1,
    max_papers_per_hop: int = 5,
) -> dict[str, Any]:
    """Multi-hop citation chain walker. Follows citation graphs to find related work.

    forward = papers that cite this one (who built on it).
    backward = papers this one cites (what it built on).
    depth = how many hops to follow (1 = direct, 2 = citing papers of citing papers).
    """
    visited: set[str] = set()
    all_papers: list[dict[str, Any]] = []
    queue: list[tuple[str, int]] = [(paper_id, 0)]

    while queue:
        current_id, current_depth = queue.pop(0)
        if current_id in visited or current_depth >= depth:
            continue
        visited.add(current_id)

        try:
            citations_data = _json_load(
                await academix_server.academic_get_citations(
                    current_id, limit=max_papers_per_hop, response_format="json"
                )
            )
        except Exception:
            continue

        citing = citations_data.get("citing_papers", []) if isinstance(citations_data, dict) else []
        for paper in citing[:max_papers_per_hop]:
            normed = _normalize_paper(paper if isinstance(paper, dict) else {"title": str(paper)}, "citation-walk")
            normed["hop"] = current_depth + 1
            normed["via"] = current_id
            all_papers.append(normed)
            next_id = (
                normed.get("doi")
                or normed.get("arxiv_id")
                or normed.get("paper_id")
                or ""
            )
            if next_id and current_depth + 1 < depth:
                queue.append((next_id, current_depth + 1))

    deduped = _merge_papers(all_papers, len(all_papers))
    return {
        "root_paper": paper_id,
        "direction": direction,
        "depth": depth,
        "total_found": len(deduped),
        "papers": deduped,
    }


@mcp.tool()
async def author_literature(
    author_name: str,
    year_from: int | None = None,
    year_to: int | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    """Find papers by a specific author, with optional year filters."""
    output = await academix_server.academic_search_author(
        author_name=author_name,
        year_from=year_from,
        year_to=year_to,
        limit=max(1, min(100, limit)),
        response_format="json",
    )
    return _json_load(output)


@mcp.tool()
async def export_bibliography(paper_ids: list[str], use_dblp: bool = True) -> str:
    """Export BibTeX for DOI, arXiv, OpenAlex, DBLP, or Semantic Scholar paper IDs."""
    return await academix_server.academic_get_bibtex(
        paper_ids=",".join(paper_ids),
        use_dblp=use_dblp,
    )


@mcp.tool()
async def search_specific_sources(
    query: str,
    sources: str,
    max_results_per_source: int = 10,
    year: str | None = None,
) -> dict[str, Any]:
    """Directly search named databases when source control matters.

    Sources: arxiv, semantic, openalex, crossref, dblp, pubmed, pmc, europepmc,
    biorxiv, medrxiv, core, openaire, doaj, base, hal, zenodo, ssrn, unpaywall,
    scopus, springer.

    Scopus and Springer require ELSEVIER_API_KEY and SPRINGER_API_KEY env vars.
    """
    source_list = [s.strip() for s in sources.split(",")]
    paper_search_sources = [s for s in source_list if s not in ("scopus", "springer")]
    publisher_sources = [s for s in source_list if s in ("scopus", "springer")]

    results: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}

    year_from: str | None = None
    year_to: str | None = None
    if year and "-" in year:
        parts = year.split("-")
        if parts[0]:
            year_to = parts[0]
        if len(parts) > 1 and parts[1]:
            year_from = parts[1]

    # Paper-search backends
    if paper_search_sources:
        try:
            ps_result = await paper_search.search_papers(
                query=query,
                max_results_per_source=max(1, min(30, max_results_per_source)),
                sources=",".join(paper_search_sources),
                year=year,
            )
            for key, papers in (ps_result.get("source_results") or {}).items():
                results[key] = papers
            if ps_result.get("errors"):
                errors.update(ps_result["errors"])
        except Exception as exc:
            errors["paper_search"] = str(exc)

    # Publisher API backends
    for src in publisher_sources:
        try:
            if src == "scopus":
                papers = await search_scopus(
                    query, max_results=max_results_per_source,
                    year_from=year_from, year_to=year_to,
                )
            elif src == "springer":
                papers = await search_springer(
                    query, max_results=max_results_per_source,
                    year_from=year_from, year_to=year_to,
                )
            else:
                continue
            results[src] = papers
        except Exception as exc:
            errors[src] = str(exc)

    # Flatten and merge
    all_papers: list[dict[str, Any]] = []
    for src_papers in results.values():
        all_papers.extend(src_papers)

    merged = _merge_papers(all_papers, max_results_per_source * max(1, len(source_list)))

    return {
        "query": query,
        "sources_requested": source_list,
        "sources_used": list(results.keys()),
        "source_results": {k: v for k, v in results.items() if v},
        "errors": errors,
        "papers": merged,
        "total": len(merged),
    }


# ---------------------------------------------------------------------------
# Full-text tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def read_paper(
    paper_id: str,
    source: str = "auto",
    doi: str = "",
    title: str = "",
    save_path: str = "./downloads",
    use_scihub: bool = True,
) -> dict[str, Any]:
    """Download and extract full text from a paper.

    source = "auto" auto-detects from paper_id format. Explicit sources: arxiv, semantic, biorxiv, medrxiv, iacr, openaire, citeseerx, doaj, base, zenodo, hal.
    Falls back through: native source → OA repositories → Unpaywall → Sci-Hub (when enabled).
    Returns extracted text content plus metadata.
    """
    if source == "auto":
        source = _detect_source_from_paper({
            "arxiv_id": paper_id, "doi": doi, "paper_id": paper_id,
            "sources": [paper_id],
        })

    result: dict[str, Any] = {"paper_id": paper_id, "source": source}

    readers = {
        "arxiv": paper_search.read_arxiv_paper,
        "semantic": paper_search.read_semantic_paper,
        "biorxiv": paper_search.read_biorxiv_paper,
        "medrxiv": paper_search.read_medrxiv_paper,
        "iacr": paper_search.read_iacr_paper,
        "openaire": paper_search.read_openaire_paper,
        "citeseerx": paper_search.read_citeseerx_paper,
        "doaj": paper_search.read_doaj_paper,
        "base": paper_search.read_base_paper,
        "zenodo": paper_search.read_zenodo_paper,
        "hal": paper_search.read_hal_paper,
    }

    reader = readers.get(source)
    if reader:
        try:
            text = await reader(paper_id, save_path=save_path)
            result["text"] = text
            result["success"] = bool(text)
            return result
        except Exception as exc:
            result["reader_error"] = str(exc)

    try:
        path = await paper_search.download_with_fallback(
            source=source, paper_id=paper_id, doi=doi, title=title,
            save_path=save_path, use_scihub=use_scihub,
        )
        result["download_path"] = path
        result["success"] = path is not None
    except Exception as exc:
        result["download_error"] = str(exc)
        result["success"] = False

    # Springer OA fallback (if DOI provided and main download failed)
    if not result.get("success") and doi:
        try:
            oa_url = await springer_resolve_oa(doi)
            if oa_url:
                import httpx as _httpx
                async with _httpx.AsyncClient(follow_redirects=True) as _client:
                    resp = await _client.get(oa_url, timeout=30.0)
                    if resp.status_code == 200 and "pdf" in resp.headers.get("content-type", "") or resp.content[:5] == b"%PDF-":
                        from pathlib import Path as _P
                        out_dir = _P(save_path)
                        out_dir.mkdir(parents=True, exist_ok=True)
                        out_file = out_dir / f"{paper_id.replace('/', '_')}.pdf"
                        out_file.write_bytes(resp.content)
                        result["download_path"] = str(out_file)
                        result["success"] = True
                        result["springer_oa_url"] = oa_url
        except Exception:
            pass

    return result


@mcp.tool()
async def search_scihub(
    identifier: str,
    save_path: str = "./downloads",
) -> dict[str, Any]:
    """Download a paper via Sci-Hub by DOI, title, PMID, or URL.

    Sci-Hub is a Legal gray area — use at your own risk.
    Works best with DOIs. Falls back through multiple Sci-Hub mirrors.
    """
    result: dict[str, Any] = {"identifier": identifier}
    try:
        path = await paper_search.download_scihub(identifier, save_path=save_path)
        result["download_path"] = path
        result["success"] = path is not None
    except Exception as exc:
        result["error"] = str(exc)
        result["success"] = False
    return result


@mcp.tool()
async def batch_read(
    papers: list[dict[str, Any]],
    save_path: str = "./downloads",
    max_concurrent: int = 3,
) -> dict[str, Any]:
    """Download and extract full text for multiple papers concurrently.

    Each paper dict should have at minimum: title + one of (doi, arxiv_id, paper_id).
    Returns dict with success/failure counts and extracted texts keyed by paper title.
    """
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _read_one(paper: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        title = paper.get("title", "unknown")
        async with semaphore:
            paper_id = (
                paper.get("arxiv_id")
                or paper.get("doi")
                or paper.get("paper_id")
                or title
            )
            source = _detect_source_from_paper(paper)
            try:
                result = await read_paper(
                    paper_id=paper_id,
                    source=source or "auto",
                    doi=paper.get("doi", ""),
                    title=title,
                    save_path=save_path,
                )
                return title, result
            except Exception as exc:
                return title, {"success": False, "error": str(exc)}

    tasks = [_read_one(p) for p in papers if p.get("title") or p.get("doi")]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    texts: dict[str, Any] = {}
    succeeded = 0
    failed = 0
    for r in results:
        if isinstance(r, Exception):
            failed += 1
            continue
        title, detail = r
        texts[title] = detail
        if detail.get("success"):
            succeeded += 1
        else:
            failed += 1

    return {
        "total": len(papers),
        "succeeded": succeeded,
        "failed": failed,
        "texts": texts,
    }


# ---------------------------------------------------------------------------
# Curation tools
# ---------------------------------------------------------------------------

@mcp.tool()
def curate_research(
    action: Literal[
        "rank",
        "filter_duplicates",
        "pool_status",
        "prepare_review",
        "prepare_summarize",
        "list_topics",
    ],
    papers: list[dict[str, Any]] | None = None,
    top_n: int = 10,
    custom_focus: str = "",
) -> Any:
    """Paper Distill curation utilities for ranking, duplicate filtering, pool status, and review prompts."""
    if action == "rank":
        return paper_distill.rank_papers(papers or [], top_n=top_n)
    if action == "filter_duplicates":
        return paper_distill.filter_duplicates(papers or [])
    if action == "pool_status":
        return paper_distill.pool_status()
    if action == "prepare_review":
        return paper_distill.prepare_review()
    if action == "prepare_summarize":
        return paper_distill.prepare_summarize(custom_focus=custom_focus)
    if action == "list_topics":
        return paper_distill.manage_topics(action="list")
    raise ValueError(f"Unsupported action: {action}")


@mcp.tool()
async def paper_distill_pipeline(
    action: Literal[
        "setup",
        "init_session",
        "load_context",
        "add_topic",
        "configure",
        "refresh_pool",
        "finalize_review",
        "collect",
    ],
    payload: dict[str, Any] | None = None,
) -> Any:
    """Progressive access to Paper Distill workflow actions.

    Use only when managing a recurring paper-review/distillation workflow, Zotero collection,
    topic preferences, or push digests. For ordinary search, use search_literature instead.
    """
    payload = payload or {}
    if action == "setup":
        return paper_distill.setup()
    if action == "init_session":
        return paper_distill.init_session(**payload)
    if action == "load_context":
        return paper_distill.load_session_context(**payload)
    if action == "add_topic":
        return paper_distill.add_topic(**payload)
    if action == "configure":
        return paper_distill.configure(**payload)
    if action == "refresh_pool":
        return await paper_distill.pool_refresh(**payload)
    if action == "finalize_review":
        return paper_distill.finalize_review(**payload)
    if action == "collect":
        return paper_distill.collect(**payload)
    raise ValueError(f"Unsupported action: {action}")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
