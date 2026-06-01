"""Bundled academic research MCP server.

Integrates Academix (metadata + citations), Paper Search (21+ sources + PDF/text),
and Paper Distill (curation pipeline) into one compact tool surface.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from publisher_apis import search_scopus, search_springer, springer_resolve_oa

from fastmcp import FastMCP  # noqa: E402

from academix.aggregator import AcademicAggregator  # noqa: E402
from academix import server as academix_server  # noqa: E402
from paper_search_mcp import server as paper_search  # noqa: E402

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
# Caching layer
# ---------------------------------------------------------------------------

class _TTLCache:
    """Simple in-memory TTL cache keyed by content hash."""

    def __init__(self, ttl_seconds: int = 600, max_entries: int = 512):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: dict[str, tuple[float, Any]] = {}

    def _key(self, *args: Any, **kwargs: Any) -> str:
        blob = json.dumps(args, sort_keys=True, default=str) + json.dumps(
            kwargs, sort_keys=True, default=str
        )
        return hashlib.md5(blob.encode()).hexdigest()

    def get(self, *args: Any, **kwargs: Any) -> Any | None:
        k = self._key(*args, **kwargs)
        entry = self._store.get(k)
        if entry is None:
            return None
        ts, val = entry
        if time.monotonic() - ts > self._ttl:
            del self._store[k]
            return None
        return val

    def set(self, value: Any, *args: Any, **kwargs: Any) -> None:
        if len(self._store) >= self._max:
            # Evict oldest
            oldest_key = min(self._store, key=lambda k: self._store[k][0])
            del self._store[oldest_key]
        self._store[self._key(*args, **kwargs)] = (time.monotonic(), value)


_search_cache = _TTLCache(ttl_seconds=600, max_entries=256)
_lookup_cache = _TTLCache(ttl_seconds=3600, max_entries=1024)


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

async def _retry_async(
    coro_factory,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
) -> Any:
    """Execute an async callable with exponential backoff retry."""
    import random
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = min(base_delay * (2 ** attempt) + random.uniform(0, 0.5), max_delay)
                await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


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
        # Handle "Last, First" format: "Smith, John A.; Doe, Jane" -> ["John A. Smith", "Jane Doe"]
        if ";" in value or "," in value:
            parts = [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
            # If odd number of parts, first might be "Last1 First1 Last2"
            # Simple heuristic: if a part is short (<4 chars) it's likely an initial
            if len(parts) == 2:
                return [f"{parts[1]} {parts[0]}".strip()]
            return [p for p in parts if p]
        return [value.strip()]
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
    return str(value or "").strip().lower().removeprefix("https://doi.org/").removeprefix("http://doi.org/").removeprefix("doi:")


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
    # Title-based dedup: only merge if titles are very similar (not just containing same words)
    title = str(paper.get("title") or "").lower()
    import re as _re
    title = _re.sub(r"[^\w\s]", "", title)
    title = " ".join(title.split())
    year = str(paper.get("year") or paper.get("published_date") or "")[:4]
    # Use first 80 chars of normalized title to avoid merging different papers with shared prefixes
    return f"title:{title[:80]}:{year}"


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
    # Detect OA status: explicit flag, or has pdf_url, or from OA sources
    is_oa = (
        p.get("is_open_access")
        or bool(p.get("pdf_url") or p.get("open_access_url"))
        or source in ("unpaywall", "doaj", "openaire", "base", "zenodo", "hal")
        or (p.get("arxiv_id") or p.get("arxiv"))
        or "biorxiv" in str(p.get("doi") or "")
        or "medrxiv" in str(p.get("doi") or "")
    )
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
        "is_open_access": bool(is_oa),
        "sources": sorted(set(s for s in [source, str(p.get("source") or source)] if s)),
    }


SOURCE_PRECISION_BONUS = {
    "openaire": 2,
    "academix": 1,
    "arxiv": 1,
    "semantic": 1,
    "scopus": 2,
    "springer": 2,
    "europepmc": 0,
    "pubmed": 0,
    "crossref": 0,
    "unpaywall": 0,
}


def _merge_papers(items: list[dict[str, Any]], limit: int, query: str | None = None) -> list[dict[str, Any]]:
    """Deduplicate and rank papers. Adds relevance_score when query is provided.

    relevance_score = term overlap between query and title (0-10 scale)
    + citation boost (1-3 points for 50+/100+/500+ citations).
    """
    import re as _re
    # Extract query terms for relevance scoring
    query_terms = set()
    if query:
        q_clean = _re.sub(r"[^\w\s]", "", query.lower())
        query_terms = {w for w in q_clean.split() if len(w) > 2}

    merged: dict[str, dict[str, Any]] = {}
    for item in items:
        key = _paper_key(item)
        if not key or key.startswith("title::"):
            continue
        existing = merged.get(key)
        if existing is None:
            raw_hits = len(set(item.get("sources") or []))
            precision_boost = 0
            for s in (item.get("sources") or []):
                precision_boost = max(precision_boost, SOURCE_PRECISION_BONUS.get(s, 0))
            item["source_hits"] = raw_hits + precision_boost
            # Compute relevance score from title term overlap + citation boost
            score = 0
            if query_terms and item.get("title"):
                title_clean = _re.sub(r"[^\w\s]", "", item["title"].lower())
                title_terms = set(title_clean.split())
                overlap = len(title_terms & query_terms)
                score = min(overlap * 3, 10)
            # Boost papers with high citation counts (top 10% of cited works get +3)
            cites = _to_int(item.get("citation_count"))
            if cites >= 500:
                score = min(score + 3, 10)
            elif cites >= 100:
                score = min(score + 2, 10)
            elif cites >= 50:
                score = min(score + 1, 10)
            item["relevance_score"] = score
            merged[key] = item
            continue
        new_sources = set(existing.get("sources") or []) | set(item.get("sources") or [])
        existing["sources"] = sorted(new_sources)
        raw_hits = len(new_sources)
        precision_boost = 0
        for s in new_sources:
            precision_boost = max(precision_boost, SOURCE_PRECISION_BONUS.get(s, 0))
        existing["source_hits"] = raw_hits + precision_boost
        for field in ("abstract", "doi", "arxiv_id", "pmid", "paper_id", "url", "pdf_url", "venue", "year", "keywords"):
            if not existing.get(field) and item.get(field):
                existing[field] = item[field]
        existing["citation_count"] = max(
            _to_int(existing.get("citation_count")), _to_int(item.get("citation_count"))
        )
        # Update relevance score if this version has better title match
        if "relevance_score" not in existing or item.get("relevance_score", 0) > existing.get("relevance_score", 0):
            existing["relevance_score"] = item.get("relevance_score", 0)
    # Rank by: source_hits, relevance_score, has_abstract, citation_count, then year as tiebreaker
    ranked = sorted(
        merged.values(),
        key=lambda p: (
            _to_int(p.get("source_hits")),
            _to_int(p.get("relevance_score")),
            1 if p.get("abstract") else 0,
            min(_to_int(p.get("citation_count")), 5000),
            _to_int(p.get("year")),
        ),
        reverse=True,
    )
    # Filter out papers with relevance_score < 3 (no query term match + no citation boost)
    # Score 0 = zero query terms in title AND <50 citations
    # Score 1-2 = only from citation boost with no title match (rare)
    ranked = [p for p in ranked if _to_int(p.get("relevance_score")) >= 3]
    return ranked[:limit]


BEST_SOURCES = "arxiv,semantic,crossref,unpaywall,openaire"


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
    expansions_added = 0
    for word in words:
        if word in acronyms and expansions_added < 2:
            for expansion in acronyms[word][:1]:  # Take only first expansion per acronym
                new_q = query.lower().replace(word, expansion)
                if new_q not in expansions:
                    expansions.append(new_q)
                    expansions_added += 1
                    break  # One expansion per acronym
    return expansions[:3]  # Original + up to 2 expansions


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
    # Check PMC before Semantic Scholar for PMIDs
    pmid = str(paper.get("pmid") or "")
    if pmid:
        return "pmc"  # PMC has actual full text
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
    max_results: int = 15,
    year_from: int | None = None,
    year_to: int | None = None,
    expand_queries: bool = True,
    auto_cite_walk: bool = True,
    cite_walk_depth: int = 2,
    cite_walk_max_papers: int = 5,
    check_scihub: bool = False,
) -> dict[str, Any]:
    """Search academic papers across 8 sources (arXiv, Semantic Scholar, OpenAlex, CrossRef, Unpaywall, OpenAIRE, Scopus, Springer). Returns deduplicated results with abstracts, citation counts, and auto-walks citation graphs."""
    # Check cache
    cache_key = (query, max_results, year_from, year_to, expand_queries, auto_cite_walk)
    cached = _search_cache.get(*cache_key)
    if cached is not None:
        return cached

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
                max_results_per_source=max(5, min(max_results, 20)),
                sources=BEST_SOURCES,
                year=year,
            ),
        ])
        # Add publisher APIs when keys are set
        if os.environ.get("ELSEVIER_API_KEY"):
            tasks.append(search_scopus(q, max_results=max_results, year_from=str(year_from) if year_from else None, year_to=str(year_to) if year_to else None))
        if os.environ.get("SPRINGER_API_KEY"):
            tasks.append(search_springer(q, max_results=max_results, year_from=str(year_from) if year_from else None, year_to=str(year_to) if year_to else None))

    # Use gather without wait_for to salvage partial results on timeout
    outputs = await asyncio.gather(*tasks, return_exceptions=True)

    papers: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    num_backends = 2 + bool(os.environ.get("ELSEVIER_API_KEY")) + bool(os.environ.get("SPRINGER_API_KEY"))

    for i, out in enumerate(outputs):
        q_idx = i // num_backends
        backend = i % num_backends
        q_label = queries[q_idx] if q_idx < len(queries) else query

        if isinstance(out, Exception):
            backend_names = ["academix", "paper-search", "scopus", "springer"]
            errors[f"{backend_names[backend]}_{q_label}"] = str(out)
            continue

        if backend == 0:
            data = _json_load(out)
            for paper in data.get("papers", []) if isinstance(data, dict) else []:
                p = _normalize_paper(paper, "academix")
                p["source_hits"] = max(_to_int(p.get("source_hits")), 2)  # Boost: OpenAlex ranking is proven
                papers.append(p)
        elif backend == 1:
            data = _json_load(out)
            for paper in data.get("papers", []) if isinstance(data, dict) else []:
                papers.append(_normalize_paper(paper, "paper-search"))
        elif backend == 2:
            for paper in (out or []) if isinstance(out, list) else []:
                papers.append(_normalize_paper(paper, "scopus"))
        elif backend == 3:
            for paper in (out or []) if isinstance(out, list) else []:
                papers.append(_normalize_paper(paper, "springer"))

    merged = _merge_papers(papers, max_results, query)

    result = {
        "query": query,
        "queries_used": queries,
        "total_before_dedupe": len(papers),
        "returned": len(merged),
        "errors": errors,
        "papers": merged,
    }

    if auto_cite_walk and merged:
        # Walk by citation count — ensures highly-cited papers are walked, which
        # surfaces their references (classics) and citing papers (follow-on work)
        walk_candidates = sorted(merged, key=lambda p: -_to_int(p.get("citation_count")))
        walk_ids = []
        for p in walk_candidates[:cite_walk_max_papers]:
            if p.get("source_hits", 0) >= 2:  # Only walk papers found by multiple sources
                pid = p.get("doi") or p.get("arxiv_id") or p.get("paper_id")
                if pid:
                    walk_ids.append(pid)

        async def _fetch_citations_s2(pid: str) -> list[dict[str, Any]]:
            """Fetch citing papers from Semantic Scholar."""
            try:
                citations_data = _json_load(
                    await academix_server.academic_get_citations(
                        pid, limit=10, response_format="json"
                    )
                )
                citing = citations_data.get("citing_papers", []) if isinstance(citations_data, dict) else []
                return [_normalize_paper(cp, "citation-walk-s2") for cp in citing[:10]]
            except Exception:
                return []

        async def _fetch_citations_oa(pid: str) -> list[dict[str, Any]]:
            """Fetch citing papers from OpenAlex (free, no rate limits)."""
            try:
                from publisher_apis import _get_client
                client = await _get_client()
                oa_email = os.environ.get("UNPAYWALL_EMAIL", "research@example.com")
                doi = pid if pid.startswith("10.") else ""
                if not doi:
                    return []
                resp = await client.get(
                    f"https://api.openalex.org/works",
                    params={
                        "filter": f"cites:{doi}",
                        "per_page": 10,
                        "mailto": oa_email,
                    },
                )
                if resp.status_code != 200:
                    return []
                data = resp.json()
                results = data.get("results", [])
                papers = []
                for r in results[:10]:
                    authors = []
                    for a in (r.get("authorships") or [])[:5]:
                        name = (a.get("author") or {}).get("display_name", "")
                        if name:
                            authors.append(name)
                    abstract_inv = r.get("abstract_inverted_index")
                    abstract = ""
                    if abstract_inv:
                        positions = []
                        for word, pos_list in abstract_inv.items():
                            for pos in pos_list:
                                positions.append((pos, word))
                        positions.sort()
                        abstract = " ".join(w for _, w in positions)
                    papers.append({
                        "title": r.get("title", ""),
                        "authors": authors,
                        "year": (r.get("publication_date") or "")[:4] or None,
                        "doi": r.get("doi", ""),
                        "abstract": abstract,
                        "citation_count": _to_int((r.get("cited_by_count") or 0)),
                        "url": r.get("id", ""),
                    })
                return [_normalize_paper(p, "citation-walk-oa") for p in papers]
            except Exception:
                return []

        async def _fetch_references_oa(pid: str) -> list[dict[str, Any]]:
            """Fetch references (backward) from OpenAlex."""
            try:
                from publisher_apis import _get_client
                client = await _get_client()
                oa_email = os.environ.get("UNPAYWALL_EMAIL", "research@example.com")
                doi = pid if pid.startswith("10.") else ""
                if not doi:
                    return []
                resp = await client.get(
                    f"https://api.openalex.org/works/doi:{doi}",
                    params={"mailto": oa_email},
                )
                if resp.status_code != 200:
                    return []
                data = resp.json()
                refs = data.get("referenced_works", [])
                if not refs:
                    return []
                # Batch fetch referenced works
                ids_param = "|".join(refs[:20])
                ref_resp = await client.get(
                    "https://api.openalex.org/works",
                    params={"filter": f"ids:{ids_param}", "per_page": 20, "mailto": oa_email},
                )
                if ref_resp.status_code != 200:
                    return []
                results = ref_resp.json().get("results", [])
                papers = []
                for r in results[:10]:
                    authors = []
                    for a in (r.get("authorships") or [])[:5]:
                        name = (a.get("author") or {}).get("display_name", "")
                        if name:
                            authors.append(name)
                    abstract_inv = r.get("abstract_inverted_index")
                    abstract = ""
                    if abstract_inv:
                        positions = []
                        for word, pos_list in abstract_inv.items():
                            for pos in pos_list:
                                positions.append((pos, word))
                        positions.sort()
                        abstract = " ".join(w for _, w in positions)
                    papers.append({
                        "title": r.get("title", ""),
                        "authors": authors,
                        "year": (r.get("publication_date") or "")[:4] or None,
                        "doi": r.get("doi", ""),
                        "abstract": abstract,
                        "citation_count": _to_int((r.get("cited_by_count") or 0)),
                        "url": r.get("id", ""),
                    })
                return [_normalize_paper(p, "reference-walk-oa") for p in papers]
            except Exception:
                return []

        # Fetch forward (citing) and backward (references) in parallel
        citation_tasks = []
        for pid in walk_ids:
            citation_tasks.append(_fetch_citations_s2(pid))
            citation_tasks.append(_fetch_citations_oa(pid))
            citation_tasks.append(_fetch_references_oa(pid))

        citation_results = await asyncio.gather(
            *citation_tasks, return_exceptions=True,
        )
        citation_papers: list[dict[str, Any]] = []
        for r in citation_results:
            if isinstance(r, list):
                citation_papers.extend(r)

        if citation_papers:
            # Citation walk papers get source_hits floor of 2 so they compete in ranking
            for cp in citation_papers:
                cp["citation_walk"] = True
                cp["source_hits"] = max(_to_int(cp.get("source_hits")), 2)
                # Recompute relevance_score with citation boost
                cites = _to_int(cp.get("citation_count"))
                score = _to_int(cp.get("relevance_score"))
                if cites >= 500:
                    score = min(score + 3, 10)
                elif cites >= 100:
                    score = min(score + 2, 10)
                elif cites >= 50:
                    score = min(score + 1, 10)
                cp["relevance_score"] = score
            all_papers = merged + citation_papers
            result["papers"] = _merge_papers(all_papers, max_results + 10, query)
            result["citation_walk_found"] = len(citation_papers)

    if check_scihub:
        try:
            scihub_map = await _check_scihub_batch(result["papers"])
            for p in result["papers"]:
                doi = p.get("doi")
                if doi and doi in scihub_map:
                    p["scihub_available"] = scihub_map[doi]
            result["scihub_checked"] = len(scihub_map)
            result["scihub_available_count"] = sum(1 for v in scihub_map.values() if v)
        except Exception:
            pass

    _search_cache.set(result, *cache_key)
    return result


@mcp.tool()
async def walk_citations(
    paper_id: str,
    direction: Literal["forward", "backward", "both"] = "forward",
    depth: int = 1,
    max_papers_per_hop: int = 5,
) -> dict[str, Any]:
    """Follow citation graphs forward (who cites) or backward (what it cites), multi-hop."""
    visited: set[str] = set()
    all_papers: list[dict[str, Any]] = []
    queue: deque[tuple[str, int]] = deque([(paper_id, 0)])

    while queue:
        current_id, current_depth = queue.popleft()
        if current_id in visited or current_depth >= depth:
            continue
        visited.add(current_id)

        # Forward citations (who cites this paper)
        if direction in ("forward", "both"):
            try:
                citations_data = _json_load(
                    await academix_server.academic_get_citations(
                        current_id, limit=max_papers_per_hop, response_format="json"
                    )
                )
                citing = citations_data.get("citing_papers", []) if isinstance(citations_data, dict) else []
                for paper in citing[:max_papers_per_hop]:
                    normed = _normalize_paper(paper if isinstance(paper, dict) else {"title": str(paper)}, "citation-walk")
                    normed["hop"] = current_depth + 1
                    normed["via"] = current_id
                    normed["direction"] = "forward"
                    all_papers.append(normed)
                    next_id = (
                        normed.get("doi")
                        or normed.get("arxiv_id")
                        or normed.get("paper_id")
                        or ""
                    )
                    if next_id and current_depth + 1 < depth:
                        queue.append((next_id, current_depth + 1))
            except Exception:
                pass

        # Backward citations (what this paper cites)
        if direction in ("backward", "both"):
            try:
                refs_data = _json_load(
                    await academix_server.academic_get_citation_network(
                        current_id, direction="cited", max_nodes=max_papers_per_hop
                    )
                )
                edges = refs_data.get("edges", []) if isinstance(refs_data, dict) else []
                nodes = refs_data.get("nodes", []) if isinstance(refs_data, dict) else []
                # Build node lookup
                node_map = {n.get("paper_id"): n for n in nodes if isinstance(n, dict)}
                for edge in edges:
                    if isinstance(edge, dict):
                        # In "cited" direction, target is the cited paper
                        cited_id = edge.get("target")
                        if cited_id and cited_id not in visited:
                            node = node_map.get(cited_id, {})
                            normed = _normalize_paper(
                                {"paper_id": cited_id, "title": node.get("title", ""), "year": node.get("year")},
                                "citation-walk",
                            )
                            normed["hop"] = current_depth + 1
                            normed["via"] = current_id
                            normed["direction"] = "backward"
                            all_papers.append(normed)
                            if current_depth + 1 < depth:
                                queue.append((cited_id, current_depth + 1))
            except Exception:
                pass

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
async def export_references(
    papers: list[dict[str, Any]],
    format: Literal["ris", "csv", "json", "bibtex"] = "ris",
) -> str:
    """Export paper references in RIS, CSV, JSON, or BibTeX format."""
    lines: list[str] = []

    if format == "ris":
        for p in papers:
            lines.append("TY  - JOUR")
            if p.get("title"):
                lines.append(f"TI  - {p['title']}")
            for author in (p.get("authors") or []):
                lines.append(f"AU  - {author}")
            if p.get("year"):
                lines.append(f"PY  - {p['year']}")
            if p.get("doi"):
                lines.append(f"DO  - {p['doi']}")
            if p.get("venue"):
                lines.append(f"JO  - {p['venue']}")
            if p.get("abstract"):
                lines.append(f"AB  - {p['abstract']}")
            if p.get("url"):
                lines.append(f"UR  - {p['url']}")
            if p.get("arxiv_id"):
                lines.append(f"AN  - arXiv:{p['arxiv_id']}")
            lines.append("ER  - ")
            lines.append("")
        return "\n".join(lines)

    elif format == "csv":
        lines.append("title,authors,year,doi,url,venue,arxiv_id,citation_count")
        for p in papers:
            authors = "; ".join(p.get("authors") or [])
            title = (p.get("title") or "").replace(",", ";")
            lines.append(
                f'"{title}","{authors}",{p.get("year") or ""},{p.get("doi") or ""},'
                f'{p.get("url") or ""},{p.get("venue") or ""},'
                f'{p.get("arxiv_id") or ""},{p.get("citation_count") or 0}'
            )
        return "\n".join(lines)

    elif format == "json":
        return json.dumps(papers, indent=2, default=str)

    elif format == "bibtex":
        ids = [
            p.get("doi") or p.get("arxiv_id") or p.get("paper_id", "")
            for p in papers
            if p.get("doi") or p.get("arxiv_id") or p.get("paper_id")
        ]
        return await academix_server.academic_get_bibtex(
            paper_ids=",".join(ids),
        )

    return "Unsupported format"


@mcp.tool()
async def paper_lookup(
    query: str,
    max_results: int = 5,
) -> dict[str, Any]:
    """Find a paper by DOI, arXiv ID, Semantic Scholar ID, or title."""
    results: list[dict[str, Any]] = []
    errors: dict[str, str] = {}

    # Detect if query is a DOI, arXiv ID, or title
    q = query.strip()
    is_id = (
        q.startswith("10.")  # DOI
        or q.startswith("arxiv:")  # arXiv prefix
        or (q.startswith("http") and "doi.org" in q)  # DOI URL
        or (q.replace(".", "").replace("-", "").isdigit() and len(q) > 5)  # pure numeric ID
        or q.startswith("W")  # OpenAlex ID
    )

    if is_id:
        # Direct lookup by ID
        try:
            details = await _retry_async(
                lambda pid=q: academix_server.academic_get_paper_details(pid, response_format="json")
            )
            parsed = _json_load(details)
            if isinstance(parsed, dict) and parsed.get("title"):
                results.append(_normalize_paper(parsed, "academix-lookup"))
        except Exception as exc:
            errors["academix_details"] = str(exc)

        # Fallback to CrossRef if academix failed
        if not results:
            try:
                cr = await paper_search.get_crossref_paper_by_doi(q)
                if cr and isinstance(cr, dict):
                    results.append(_normalize_paper(cr, "crossref-lookup"))
            except Exception:
                pass
    else:
        # Search by title across Semantic Scholar + CrossRef
        try:
            s2_results = await _retry_async(
                lambda t=q: academix_server.academic_search_papers(
                    query=f'title:"{t}"', sort="relevance",
                    limit=max_results, response_format="json",
                )
            )
            data = _json_load(s2_results)
            for p in data.get("papers", []) if isinstance(data, dict) else []:
                results.append(_normalize_paper(p, "semantic-scholar-title"))
        except Exception as exc:
            errors["semantic_scholar"] = str(exc)

        try:
            cr_results = await paper_search.get_crossref_paper_by_doi(q)
            if cr_results and isinstance(cr_results, dict):
                results.append(_normalize_paper(cr_results, "crossref-title"))
        except Exception:
            pass

    merged = _merge_papers(results, max_results, query)
    return {"query": query, "results_found": len(merged), "errors": errors, "papers": merged}


# ---------------------------------------------------------------------------
# Full-text tools
# ---------------------------------------------------------------------------

async def _check_scihub_batch(papers: list[dict[str, Any]]) -> dict[str, bool]:
    """Check Sci-Hub availability for papers with DOIs (non-blocking, best-effort).

    Uses a single shared httpx client for all requests. Deduplicates DOIs.
    """
    import httpx as _httpx

    dois = list({p["doi"] for p in papers if p.get("doi")})
    if not dois:
        return {}

    sci_hub_urls = [
        "https://sci-hub.se",
        "https://sci-hub.st",
        "https://sci-hub.ru",
    ]
    availability: dict[str, bool] = {}

    async with _httpx.AsyncClient(follow_redirects=True, timeout=8.0) as client:
        async def _check_one(doi: str) -> tuple[str, bool]:
            for base in sci_hub_urls:
                try:
                    resp = await client.get(f"{base}/{doi}")
                    if resp.status_code == 200:
                        ct = resp.headers.get("content-type", "").lower()
                        if "pdf" in ct or resp.content[:5] == b"%PDF-":
                            return doi, True
                except Exception:
                    continue
            return doi, False

        # Check all DOIs in parallel (with semaphore to avoid hammering)
        sem = asyncio.Semaphore(5)

        async def _guarded(doi: str) -> tuple[str, bool]:
            async with sem:
                return await _check_one(doi)

        tasks = [_guarded(doi) for doi in dois]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, tuple):
                availability[r[0]] = r[1]
    return availability


@mcp.tool()
async def read_paper(
    paper_id: str,
    source: str = "auto",
    doi: str = "",
    title: str = "",
    save_path: str = "./downloads",
    use_scihub: bool = True,
) -> dict[str, Any]:
    """Download and extract full text from a paper. Falls back through OA repositories, Unpaywall, Sci-Hub."""
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

    # Additional OA fallbacks (if DOI provided and main download failed)
    if not result.get("success") and doi:
        from publisher_apis import _get_client
        client = await _get_client()

        # Try OpenAlex OA URL (has open_access.oa_url for many papers)
        try:
            oa_url = None
            resp = await client.get(
                f"https://api.openalex.org/works/doi:{doi}",
                params={"mailto": os.environ.get("UNPAYWALL_EMAIL", "research@example.com")},
            )
            if resp.status_code == 200:
                data = resp.json()
                oa_info = data.get("open_access", {})
                oa_url = oa_info.get("oa_url")
                if not oa_url:
                    primary = data.get("primary_location", {})
                    if primary and primary.get("pdf_url"):
                        oa_url = primary["pdf_url"]
            if oa_url:
                pdf_resp = await client.get(oa_url, timeout=30.0)
                if pdf_resp.status_code == 200 and (
                    "pdf" in pdf_resp.headers.get("content-type", "")
                    or pdf_resp.content[:5] == b"%PDF-"
                ):
                    from pathlib import Path as _P
                    out_dir = _P(save_path)
                    out_dir.mkdir(parents=True, exist_ok=True)
                    out_file = out_dir / f"{paper_id.replace('/', '_')}.pdf"
                    out_file.write_bytes(pdf_resp.content)
                    result["download_path"] = str(out_file)
                    result["success"] = True
                    result["oa_source"] = "openalex"
                    return result
        except Exception:
            pass

        # Try Springer OA
        try:
            oa_url = await springer_resolve_oa(doi)
            if oa_url:
                pdf_resp = await client.get(oa_url, timeout=30.0)
                if pdf_resp.status_code == 200 and (
                    "pdf" in pdf_resp.headers.get("content-type", "")
                    or pdf_resp.content[:5] == b"%PDF-"
                ):
                    from pathlib import Path as _P
                    out_dir = _P(save_path)
                    out_dir.mkdir(parents=True, exist_ok=True)
                    out_file = out_dir / f"{paper_id.replace('/', '_')}.pdf"
                    out_file.write_bytes(pdf_resp.content)
                    result["download_path"] = str(out_file)
                    result["success"] = True
                    result["oa_source"] = "springer"
                    return result
        except Exception:
            pass

        # Try multi-mirror Sci-Hub (if enabled)
        if use_scihub:
            import httpx as _httpx
            sci_hub_urls = ["https://sci-hub.se", "https://sci-hub.st", "https://sci-hub.ru"]
            for mirror in sci_hub_urls:
                try:
                    async with _httpx.AsyncClient(follow_redirects=True, timeout=15.0) as sc:
                        resp = await sc.get(f"{mirror}/{doi}")
                        if resp.status_code == 200:
                            ct = resp.headers.get("content-type", "").lower()
                            if "pdf" in ct or resp.content[:5] == b"%PDF-":
                                from pathlib import Path as _P
                                out_dir = _P(save_path)
                                out_dir.mkdir(parents=True, exist_ok=True)
                                out_file = out_dir / f"{paper_id.replace('/', '_')}.pdf"
                                out_file.write_bytes(resp.content)
                                result["download_path"] = str(out_file)
                                result["success"] = True
                                result["oa_source"] = f"scihub:{mirror}"
                                return result
                except Exception:
                    continue

    return result


def _extract_sections_from_text(text: str, sections: list[str]) -> dict[str, str]:
    """Extract specific sections from paper full text using heading patterns."""
    import re

    # Common section heading patterns in academic papers
    section_patterns = {
        "abstract": r"(?i)^\s*(?:#{1,3}\s*)?(?:abstract|summary)\s*$",
        "introduction": r"(?i)^\s*(?:#{1,3}\s*)?(?:\d+\.?\s*)?introduction\s*$",
        "methods": r"(?i)^\s*(?:#{1,3}\s*)?(?:\d+\.?\s*)?(?:methods?|methodology|materials?\s+(?:and|&)\s+methods?|experimental(?:\s+(?:setup|procedure|section))?|approach)\s*$",
        "results": r"(?i)^\s*(?:#{1,3}\s*)?(?:\d+\.?\s*)?(?:results?|findings?|experiments?|evaluation|empirical\s+results?)\s*$",
        "discussion": r"(?i)^\s*(?:#{1,3}\s*)?(?:\d+\.?\s*)?(?:discussion|analysis|interpretation)\s*$",
        "conclusions": r"(?i)^\s*(?:#{1,3}\s*)?(?:\d+\.?\s*)?(?:conclusions?|concluding\s+remarks?|summary\s+and\s+conclusions?|final\s+remarks?)\s*$",
        "related_work": r"(?i)^\s*(?:#{1,3}\s*)?(?:\d+\.?\s*)?(?:related\s+work|background|prior\s+work|previous\s+work|literature\s+review|survey)\s*$",
        "limitations": r"(?i)^\s*(?:#{1,3}\s*)?(?:\d+\.?\s*)?(?:limitations?|threats|validity)\s*$",
        "future_work": r"(?i)^\s*(?:#{1,3}\s*)?(?:\d+\.?\s*)?(?:future\s+work|future\s+directions?|outlook|next\s+steps?)\s*$",
    }

    # Build section index: find all heading positions
    lines = text.split("\n")
    headings: list[tuple[int, str, str]] = []  # (line_idx, section_key, raw_heading)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or len(stripped) > 200:
            continue
        for key, pattern in section_patterns.items():
            if re.match(pattern, stripped):
                headings.append((i, key, stripped))
                break

    if not headings:
        # No structured headings found — try to extract abstract at minimum
        abstract_match = re.search(
            r"(?i)abstract[:\s]*(.{200,2000}?)(?:\n\s*\n|introduction|keywords|1\.)",
            text,
            re.DOTALL,
        )
        result = {}
        if "abstract" in sections and abstract_match:
            result["abstract"] = abstract_match.group(1).strip()
        if not result:
            result["_note"] = "No structured section headings found in paper text"
        return result

    # Extract requested sections
    extracted: dict[str, str] = {}
    for i, (line_idx, section_key, _) in enumerate(headings):
        if section_key not in sections:
            continue
        # Find the end of this section (next heading or end of text)
        if i + 1 < len(headings):
            end_idx = headings[i + 1][0]
        else:
            end_idx = len(lines)
        section_text = "\n".join(lines[line_idx + 1 : end_idx]).strip()
        # Truncate very long sections
        if len(section_text) > 3000:
            section_text = section_text[:3000] + "\n...[truncated]"
        extracted[section_key] = section_text

    return extracted


@mcp.tool()
async def extract_sections(
    paper_id: str,
    sections: list[str] | None = None,
    source: str = "auto",
    doi: str = "",
    title: str = "",
) -> dict[str, Any]:
    """Extract specific sections (abstract, intro, methods, results, discussion, conclusions, related_work, limitations, future_work) without reading the full paper."""
    if sections is None:
        sections = ["abstract", "methods", "conclusions"]

    # Read the paper
    read_result = await read_paper(
        paper_id=paper_id, source=source, doi=doi, title=title,
    )

    if not read_result.get("success") or not read_result.get("text"):
        return {
            "paper_id": paper_id,
            "success": False,
            "error": read_result.get("error") or read_result.get("download_error") or "Could not retrieve full text",
            "extracted": {},
        }

    text = read_result["text"]
    extracted = _extract_sections_from_text(text, sections)

    return {
        "paper_id": paper_id,
        "success": True,
        "source": read_result.get("source"),
        "sections_requested": sections,
        "sections_found": list(extracted.keys()),
        "extracted": extracted,
    }


@mcp.tool()
async def compare_papers(
    papers: list[dict[str, Any]],
    aspects: list[str] | None = None,
) -> dict[str, Any]:
    """Compare multiple papers side-by-side on specific aspects (method, finding, limitation, etc). Returns a comparison table."""
    if aspects is None:
        aspects = ["method", "finding", "limitation"]

    aspect_to_section = {
        "method": "methods",
        "finding": "results",
        "limitation": "limitations",
        "conclusion": "conclusions",
        "related_work": "related_work",
        "future_work": "future_work",
        "abstract": "abstract",
    }

    sections_needed = list(set(
        aspect_to_section.get(a, a) for a in aspects
    ))

    comparisons: list[dict[str, Any]] = []

    for paper in papers:
        paper_id = (
            paper.get("arxiv_id")
            or paper.get("doi")
            or paper.get("paper_id")
            or paper.get("title", "")
        )
        extract_result = await extract_sections(
            paper_id=paper_id,
            sections=sections_needed,
            doi=paper.get("doi", ""),
            title=paper.get("title", ""),
        )

        # Map extracted sections back to requested aspects
        aspect_data: dict[str, str] = {}
        for a in aspects:
            section_key = aspect_to_section.get(a, a)
            content = extract_result.get("extracted", {}).get(section_key, "")
            if content:
                # Truncate for comparison
                if len(content) > 800:
                    content = content[:800] + "..."
                aspect_data[a] = content
            else:
                aspect_data[a] = "[not found]"

        comparisons.append({
            "title": paper.get("title", "Unknown"),
            "doi": paper.get("doi"),
            "year": paper.get("year"),
            "success": extract_result.get("success", False),
            "aspects": aspect_data,
        })

    return {
        "aspect_count": len(aspects),
        "paper_count": len(comparisons),
        "aspects": aspects,
        "comparisons": comparisons,
    }


# ---------------------------------------------------------------------------
# Curation tools
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
