"""Bundled academic research MCP server.

Integrates Academix (metadata + citations), Paper Search (21+ sources + PDF/text),
and Paper Distill (curation pipeline) into one compact tool surface.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import sys
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urljoin

import httpx

from publisher_apis import _get_client, search_scopus, search_springer, springer_resolve_oa

from fastmcp import FastMCP  # noqa: E402

from academix.aggregator import AcademicAggregator  # noqa: E402
from academix import server as academix_server  # noqa: E402
from paper_search_mcp import server as paper_search  # noqa: E402

# ID format patterns (used for source auto-detection in read_paper)
ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
DOI_RE = re.compile(r"^10\.\d{4,}/\S+$")
PMID_RE = re.compile(r"^PMID:\d+$|^\d{4,9}$")
SURVEY_RE = re.compile(r"\b(review|survey|meta[- ]analysis|systematic review|umbrella review|scoping review)\b", re.IGNORECASE)

# Shared config
UNPAYWALL_EMAIL = os.environ.get("UNPAYWALL_EMAIL", "research@example.com")
PDF_MAGIC = b"%PDF-"

# Map of source name -> paper_search reader function (built once after import)
_READERS: dict[str, Any] = {}


def _get_reader(source: str):
    """Lazy-init reader registry; returns the reader callable or None."""
    if not _READERS:
        _READERS.update({
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
        })
    return _READERS.get(source)

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
    """Simple in-memory TTL cache with O(1) LRU eviction."""

    def __init__(self, ttl_seconds: int = 600, max_entries: int = 512):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()

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
        # Mark as recently used
        self._store.move_to_end(k)
        return val

    def set(self, value: Any, *args: Any, **kwargs: Any) -> None:
        k = self._key(*args, **kwargs)
        if k in self._store:
            self._store.move_to_end(k)
        self._store[k] = (time.monotonic(), value)
        if len(self._store) > self._max:
            self._store.popitem(last=False)  # O(1) LRU eviction


_search_cache = _TTLCache(ttl_seconds=600, max_entries=256)
_lookup_cache = _TTLCache(ttl_seconds=3600, max_entries=1024)


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
        if ";" in value:
            parts = [p.strip() for p in value.split(";") if p.strip()]
        elif "," in value:
            parts = [p.strip() for p in value.split(",") if p.strip()]
        else:
            return [value.strip()]
        # If exactly 2 parts, likely "Last, First"
        if len(parts) == 2:
            return [f"{parts[1]} {parts[0]}".strip()]
        return [p for p in parts if p]
    out = []
    for a in value:
        if isinstance(a, dict):
            name = (
                a.get("name")
                or a.get("author")
                or a.get("display_name")
                or a.get("givenName", "") + " " + a.get("familyName", "")
            ).strip()
            if name:
                out.append(name)
        elif a:
            out.append(str(a))
    return out


def _norm_id(value: Any) -> str:
    return str(value or "").strip().lower().removeprefix("https://doi.org/").removeprefix("http://doi.org/").removeprefix("doi:")


def _safe_filename(value: str) -> str:
    """Sanitize a string for use as a filename component (no path traversal)."""
    # Replace path separators and control chars with underscores; keep alnum, _, -, .
    s = re.sub(r"[^\w\-.]", "_", str(value or ""))
    # Strip leading dots (defeats ../ traversal even if / were not stripped)
    s = s.lstrip(".") or "paper"
    return s


def _compact_authors(authors: list[str], max_shown: int = 5) -> list[str]:
    """Keep first N authors; append a marker showing the total count."""
    if not authors or len(authors) <= max_shown:
        return list(authors or [])
    return list(authors[:max_shown]) + [f"et al. ({len(authors)} total)"]


def _compress_venue(name: str) -> str:
    """Strip publisher suffixes and conference proceedings clutter."""
    if not name:
        return ""
    n = str(name).strip()
    # Split on common publisher separators; keep the part that looks like a venue name
    for sep in (" - ", " : ", ", "):
        if sep in n:
            parts = [p.strip() for p in n.split(sep)]
            # Drop trailing parts that look like publisher suffixes
            while len(parts) > 1 and re.search(
                r"\b(press|publish|inc|ltd|corp|group|society|wiley|elsevier|springer|ieee|acm|taylor)\b",
                parts[-1], re.IGNORECASE,
            ):
                parts.pop()
            if len(parts) < len(n.split(sep)):
                n = " ".join(parts) if sep == " - " else parts[0]
                break
    # Strip common prefixes that bloat names
    n = re.sub(r"^(proceedings of the |proc\.? of the |the journal of )", "", n, flags=re.IGNORECASE)
    return n[:120]  # hard cap


# ---------------------------------------------------------------------------
# Semantic relevance (BAAI/bge-small-en-v1.5 via fastembed, ONNX, no torch)
# ---------------------------------------------------------------------------

_embedder = None
_embed_cache: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()  # text -> (ts, vec)
_EMBED_CACHE_MAX = 4096
_EMBED_CACHE_TTL = 86400  # 1 day; embeddings are stable


def _get_embedder():
    """Lazy-load the bge-small embedder. Returns None if fastembed isn't available."""
    global _embedder
    if _embedder is None:
        try:
            from fastembed import TextEmbedding
            _embedder = TextEmbedding("BAAI/bge-small-en-v1.5")
        except Exception:
            _embedder = False  # sentinel: unavailable
    return _embedder if _embedder else None


def _cached_embed(text: str) -> list[float] | None:
    """Embed text with TTL+LRU cache. Returns None if embedder unavailable."""
    model = _get_embedder()
    if model is None or not text:
        return None
    k = hashlib.md5(text.encode("utf-8")).hexdigest()
    entry = _embed_cache.get(k)
    if entry and time.monotonic() - entry[0] < _EMBED_CACHE_TTL:
        _embed_cache.move_to_end(k)
        return entry[1]
    try:
        # bge uses a query prefix for asymmetric retrieval
        vec = next(model.embed([f"Represent this sentence for searching relevant passages: {text}"])).tolist()
    except Exception:
        return None
    _embed_cache[k] = (time.monotonic(), vec)
    if len(_embed_cache) > _EMBED_CACHE_MAX:
        _embed_cache.popitem(last=False)
    return vec


def _cosine(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _passage_text(paper: dict[str, Any]) -> str:
    """Build a passage string for embedding: title + abstract (truncated)."""
    title = (paper.get("title") or "").strip()
    abstract = (paper.get("abstract") or "").strip()[:1500]
    return f"{title}. {abstract}".strip() if title or abstract else ""


# ---------------------------------------------------------------------------
# Author reputation (Semantic Scholar h-index cache, no extra latency on hit)
# ---------------------------------------------------------------------------

_author_hindex: dict[str, int] = {}  # author name -> h-index; 0 = unknown / not found
_S2_API_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
# 429 backoff: when S2 rate-limits us, skip S2 for this many seconds
_S2_QUOTA_UNTIL: float = 0.0
_S2_QUOTA_COOLDOWN = 60.0  # seconds


async def _lookup_author_hindex(name: str) -> int:
    """Look up h-index for a single author via S2. Returns 0 on failure.
    Skips S2 entirely if we are in a 429 cooldown window. Does NOT cache 0
    on rate limit (avoids poisoning the cache for the rest of the session)."""
    global _S2_QUOTA_UNTIL
    if not name or not _S2_API_KEY:
        return 0
    # Skip if we are in a 429 cooldown window
    if time.monotonic() < _S2_QUOTA_UNTIL:
        return 0
    if name in _author_hindex:
        return _author_hindex[name]
    try:
        client = await _get_client()
        # S2 author search; first match by name
        resp = await client.get(
            "https://api.semanticscholar.org/graph/v1/author/search",
            params={"query": name, "limit": 1, "fields": "hIndex"},
            headers={"x-api-key": _S2_API_KEY},
        )
        if resp.status_code == 429:
            # Don't cache — just enter cooldown
            _S2_QUOTA_UNTIL = time.monotonic() + _S2_QUOTA_COOLDOWN
            return 0
        if resp.status_code != 200:
            _author_hindex[name] = 0
            return 0
        data = resp.json()
        results = data.get("data") or []
        h = int(results[0].get("hIndex") or 0) if results else 0
    except Exception:
        h = 0
    _author_hindex[name] = h
    return h


async def _boost_authors(papers: list[dict[str, Any]]) -> None:
    """Fetch h-index for first author of each paper in parallel; mutates papers with 'first_author_h'.
    Capped at 8s to avoid blocking the main search response if S2 is slow."""
    global _S2_QUOTA_UNTIL
    names = list({(p.get("authors") or [""])[0] for p in papers if p.get("authors")})
    if not names:
        return
    # Skip entirely if in S2 cooldown
    if time.monotonic() < _S2_QUOTA_UNTIL:
        return
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(_lookup_author_hindex(n) for n in names), return_exceptions=True),
            timeout=8.0,
        )
    except asyncio.TimeoutError:
        _S2_QUOTA_UNTIL = time.monotonic() + _S2_QUOTA_COOLDOWN
        return
    h_map = {n: (r if isinstance(r, int) else 0) for n, r in zip(names, results)}
    for p in papers:
        first = (p.get("authors") or [""])[0]
        p["first_author_h"] = h_map.get(first, 0)


def _save_pdf(content: bytes, paper_id: str, save_path: str) -> str:
    """Write PDF content to disk and return the path."""
    out_dir = Path(save_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{_safe_filename(paper_id)}.pdf"
    out_file.write_bytes(content)
    return str(out_file)


def _extract_pdf_text(pdf_path: str) -> str:
    """Extract text from a PDF using pypdf (pure Python, no system deps).
    Returns the full text concatenated across all pages. Empty string on failure."""
    if not pdf_path or not Path(pdf_path).exists():
        return ""
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        parts: list[str] = []
        for i, page in enumerate(reader.pages):
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
            # Cap at ~50 pages to avoid pathological PDFs (also bounded by file size)
            if i >= 50:
                parts.append("\n\n[... truncated at 50 pages ...]")
                break
        return "\n\n".join(parts).strip()
    except Exception:
        return ""


_TITLE_STOPWORDS = {
    "the", "and", "for", "from", "with", "into", "through", "using", "about",
    "method", "scheme", "paper", "study", "analysis", "towards", "toward",
}


def _title_keywords(title: str) -> list[str]:
    """Extract significant title words used to verify that a downloaded PDF is the right paper."""
    words = re.findall(r"[a-z0-9]+", (title or "").casefold())
    out: list[str] = []
    for w in words:
        if len(w) < 4 or w in _TITLE_STOPWORDS:
            continue
        if w not in out:
            out.append(w)
    return out[:8]


def _verify_text_matches_title(text: str, title: str) -> dict[str, Any]:
    """Check whether extracted PDF text plausibly matches the requested paper title.

    This prevents false positives from repository fallbacks that return a semantically
    related but wrong PDF. If no title is supplied, verification is skipped.

    Two-stage check:
      1. Exact title substring match (whitespace-normalized) in the first 5000 chars.
         Academic papers always print the title on the first page; if a normalised
         version of the requested title appears verbatim, the PDF is almost certainly
         the right paper.
      2. Keyword overlap: at least ~70% of the significant title words (min 4) must
         appear in the first 5000 chars. This catches most wrong-paper fallbacks
         (e.g. CORE returning a same-field paper that happens to share a few words).
    """
    keywords = _title_keywords(title)
    if not keywords:
        return {"checked": False, "match": True, "reason": "no title keywords"}
    head = (text or "")[:5000]
    head_lc = head.casefold()
    head_norm = re.sub(r"\s+", " ", head_lc)
    title_norm = re.sub(r"\s+", " ", (title or "").casefold().strip())
    # Strip punctuation that often differs between title and PDF rendering.
    title_norm_clean = re.sub(r"[^\w\s]", "", title_norm)
    head_norm_clean = re.sub(r"[^\w\s]", "", head_norm)
    exact_match = bool(title_norm_clean) and title_norm_clean in head_norm_clean
    matched = [k for k in keywords if k in head_lc]
    # Stricter threshold: ~70% of significant keywords (rounded), min 4.
    needed = max(4, int(round(0.7 * len(keywords))))
    keyword_match = len(matched) >= needed
    return {
        "checked": True,
        "match": exact_match or keyword_match,
        "exact_match": exact_match,
        "keyword_match": keyword_match,
        "keywords": keywords,
        "matched": matched,
        "needed": needed,
        "matched_count": len(matched),
    }


def _pdf_result_from_path(path: str, title: str = "") -> dict[str, Any]:
    """Extract + verify text from a downloaded PDF path."""
    text = _extract_pdf_text(path)
    verification = _verify_text_matches_title(text, title)
    return {
        "text": text,
        "text_length": len(text),
        "verification": verification,
        "verified": bool(text) and verification.get("match", True),
    }


def _is_pdf_response(resp, content: bytes) -> bool:
    """Check if an HTTP response looks like a PDF."""
    return (
        "pdf" in resp.headers.get("content-type", "").lower()
        or content[:5] == PDF_MAGIC
    )


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
    # Title-based fallback: full normalized title + year for safer dedup
    title = re.sub(r"[^\w\s]", "", str(paper.get("title") or "").casefold())
    title = " ".join(title.split())
    year = str(paper.get("year") or paper.get("published_date") or "")[:4]
    return f"title:{title}:{year}" if title else ""


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _browser_candidate_urls(doi: str = "", url: str = "") -> list[str]:
    """Generate likely publisher/PDF URLs for browser_download."""
    candidates: list[str] = []
    doi = (doi or "").strip()
    if url:
        candidates.append(url.strip())
    if doi:
        d = doi.removeprefix("doi:").strip()
        d_lower = d.lower()
        candidates.append(f"https://doi.org/{d}")
        if d_lower.startswith("10.18653/v1/"):
            acl_id = d.split("/")[-1]
            candidates.extend([
                f"https://aclanthology.org/{acl_id}.pdf",
                f"https://www.aclweb.org/anthology/{acl_id}.pdf",
            ])
        if d_lower.startswith("10.1162/"):
            candidates.append(f"https://www.mitpressjournals.org/doi/pdf/{d}")
        if d_lower.startswith("10.3233/"):
            # IOS Press Argument & Computation content migrated behind SAGE DOI pages.
            safe = d.replace("/", "/")
            article_id = d_lower.split("/")[-1].replace("-", "")
            candidates.extend([
                f"https://journals.sagepub.com/doi/pdf/{safe}",
                f"https://journals.sagepub.com/doi/{safe}",
                f"https://content.iospress.com/articles/argument-and-computation/{article_id}",
                f"https://content.iospress.com/download/argument-and-computation/{article_id}/id",
            ])
        if d_lower.startswith("10.1075/"):
            suffix = d.split("/", 1)[1] if "/" in d else d
            candidates.extend([
                f"https://www.jbe-platform.com/content/journals/{d_lower}",
                f"https://www.jbe-platform.com/content/journals/{d_lower}?crawler=true",
                f"https://benjamins.com/catalog/{suffix}",
            ])
    return _dedupe_preserve_order(candidates)


def _ezproxy_url(target_url: str, ezproxy: str) -> str:
    return f"{ezproxy.rstrip('/')}/login?url={quote(target_url, safe='')}"


_browser_download_lock = asyncio.Lock()
_last_browser_request_at = 0.0


def _ledger_path(save_path: str) -> Path:
    return Path(save_path) / "download_ledger.jsonl"


def _paper_download_key(doi: str = "", title: str = "", url: str = "") -> str:
    if doi:
        return f"doi:{_norm_id(doi)}"
    if title:
        title_norm = re.sub(r"[^\w\s]", "", title.casefold())
        return "title:" + " ".join(title_norm.split())
    return f"url:{url.strip().casefold()}"


def _publisher_from_url(value: str) -> str:
    v = (value or "").casefold()
    if "sciencedirect.com" in v or "elsevier" in v:
        return "sciencedirect"
    if "springer" in v or "nature.com" in v:
        return "springer"
    if "tandfonline.com" in v or "taylorfrancis" in v:
        return "taylor-francis"
    if "sagepub.com" in v:
        return "sage"
    if "wiley" in v:
        return "wiley"
    if "jbe-platform" in v or "benjamins.com" in v:
        return "benjamins"
    if "mitpress" in v or "direct.mit.edu" in v:
        return "mitpress"
    if "aclanthology" in v or "aclweb" in v or "arxiv.org" in v:
        return "open-pdf"
    return "general"


_PUBLISHER_DELAY_SECONDS = {
    "sciencedirect": (45, 90),
    "springer": (30, 60),
    "taylor-francis": (45, 90),
    "sage": (30, 60),
    "wiley": (45, 90),
    "benjamins": (60, 120),
    "mitpress": (30, 60),
    "open-pdf": (5, 15),
    "general": (20, 45),
}


def _human_delay_seconds(publisher: str) -> float:
    lo, hi = _PUBLISHER_DELAY_SECONDS.get(publisher, _PUBLISHER_DELAY_SECONDS["general"])
    return random.uniform(lo, hi)


def _read_download_ledger(save_path: str, max_entries: int = 2000) -> list[dict[str, Any]]:
    path = _ledger_path(save_path)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_entries:]:
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:
        return []
    return out


def _append_download_ledger(save_path: str, record: dict[str, Any]) -> None:
    path = _ledger_path(save_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(record)
    row["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _find_verified_ledger_hit(save_path: str, key: str, title: str = "") -> dict[str, Any] | None:
    for rec in reversed(_read_download_ledger(save_path)):
        if rec.get("key") != key or rec.get("status") != "success":
            continue
        pdf_path = rec.get("download_path") or ""
        if not pdf_path or not Path(pdf_path).exists():
            continue
        pdf_result = _pdf_result_from_path(pdf_path, title or rec.get("title") or "")
        if pdf_result["verified"]:
            return {**rec, **pdf_result, "success": True, "reused_from_ledger": True}
    return None


def _recent_retry_block(save_path: str, key: str) -> dict[str, Any] | None:
    """Return a recent failure record that should not be retried immediately."""
    now = time.time()
    for rec in reversed(_read_download_ledger(save_path, max_entries=500)):
        if rec.get("key") != key or rec.get("status") == "success":
            continue
        try:
            retry_after = float(rec.get("retry_after_epoch") or 0)
        except (TypeError, ValueError):
            retry_after = 0
        if retry_after > now:
            return rec
    return None


def _retry_after_for_status(status: str) -> float:
    now = time.time()
    if status in ("title_mismatch", "wrong_pdf"):
        return now + 7 * 24 * 3600
    if status == "needs_login":
        return now + 10 * 60
    if status in ("timeout", "browser_timeout"):
        return now + 10 * 60
    if status in ("forbidden", "access_denied"):
        return now + 24 * 3600
    return now + 30 * 60


def _clean_abstract(value: Any) -> str:
    """Strip JATS/XML tags and clean up abstract text."""
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value))
    return re.sub(r"\s+", " ", text).strip()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _normalize_paper(paper: dict[str, Any], source: str) -> dict[str, Any]:
    p = dict(paper)
    # Only merge from "raw" sub-dict if it actually exists (avoid self-iteration)
    raw = p.pop("raw", None)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k not in p or p[k] is None:
                p[k] = v
    all_authors = _authors(p.get("authors"))
    compact_authors = _compact_authors(all_authors)
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
    sources = sorted(set(s for s in [source, str(p.get("source") or source)] if s))
    title = p.get("title") or ""
    return {
        "title": title,
        "authors": compact_authors,
        "author_count": len(all_authors),
        "year": _to_int(year_raw, 0) or None,
        "venue": _compress_venue(p.get("venue") or p.get("journal") or p.get("publisher")),
        "doi": p.get("doi"),
        "arxiv_id": p.get("arxiv_id") or p.get("arxiv"),
        "pmid": p.get("pmid"),
        "paper_id": p.get("id") or p.get("paper_id"),
        "url": p.get("url"),
        "pdf_url": p.get("pdf_url") or p.get("open_access_url"),
        "abstract": _clean_abstract(p.get("abstract")),
        "citation_count": _to_int(
            p.get("citation_count") or p.get("citations") or p.get("cited_by_count"), 0
        ),
        "keywords": p.get("keywords") or p.get("categories"),
        "is_open_access": bool(is_oa),
        "is_survey": bool(SURVEY_RE.search(title or "")),
        "sources": sources,
        "source_count": len(sources),
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

# Source tier (0-3). Higher = more trusted. Field-specific sources (tier 1)
# only count for medical/bio queries via _get_source_tier().
SOURCE_TIERS = {
    # Tier 3: high-quality bibliographic databases
    "scopus": 3, "springer": 3, "semantic": 3,
    "openalex": 3, "openalex-direct": 3, "crossref": 3,
    # Tier 2: broad but useful
    "arxiv": 2, "openaire": 2, "doaj": 2, "unpaywall": 2,
    # Tier 1: field-specific (biomedical) — only counts in medical/bio
    "europepmc": 1, "pubmed": 1, "pmc": 1, "biorxiv": 1, "medrxiv": 1, "iacr": 1,
    # Tier 0: aggregate/unknown
    "academix": 0, "paper-search": 0, "citeseerx": 0, "base": 0, "zenodo": 0, "hal": 0,
}

# Field detection patterns (priority order: medical > bio > cs > social)
_FIELD_PATTERNS = {
    "medical": [
        "patient", "clinical", "disease", "treatment", "drug", "therapy",
        "diagnosis", "hospital", "trial", "randomized", "cohort",
        "covid", "cancer", "diabetes", "cardiovascular",
    ],
    "bio": [
        "gene", "protein", "cell", "molecular", "crispr", "biology",
        "genome", "rna", "dna", "enzyme", "microbiome", "phylogenetic",
    ],
    "cs": [
        "transformer", "neural", "deep learning", "machine learning", "nlp",
        "computer vision", "llm", "language model", "neural network",
        "backpropagation", "gradient", "embedding",
    ],
    "social": [
        "education", "psychology", "sociology", "linguistics", "policy",
        "social", "classroom", "student", "teacher", "cognitive",
    ],
}

# Field-specific preferred sources (used for source-tier weighting, not API call list)
FIELD_SOURCE_BIAS = {
    "cs": ["arxiv", "semantic", "openalex", "openalex-direct", "crossref"],
    "medical": ["pubmed", "europepmc", "pmc", "semantic", "openalex", "crossref"],
    "bio": ["europepmc", "biorxiv", "medrxiv", "pmc", "semantic", "openalex"],
    "social": ["scopus", "springer", "semantic", "openalex", "crossref", "doaj"],
    "general": ["openalex", "openalex-direct", "semantic", "crossref", "arxiv"],
}


def _detect_field(query: str) -> str:
    """Auto-detect field from query. Returns: cs, medical, bio, social, or general."""
    q = (query or "").lower()
    for field in ("medical", "bio", "cs", "social"):  # priority: most specific first
        for pat in _FIELD_PATTERNS[field]:
            if pat in q:
                return field
    return "general"


def _get_source_tier(source: str, field: str) -> int:
    """Get tier for a source, given the query's field. Tier 1 biomedical sources
    only count for medical/bio fields; otherwise they're treated as tier 0."""
    base = SOURCE_TIERS.get(source, 0)
    if base == 1 and field not in ("medical", "bio"):
        return 0
    return base


def _is_rescued(paper: dict[str, Any], query: str = "") -> bool:
    """Rescue rules: keep these papers even when low-quality filter would drop them."""
    if _to_int(paper.get("citation_count")) > 500:
        return True
    if _to_int(paper.get("source_count")) >= 3:
        return True
    if paper.get("is_survey"):
        return True
    if query and paper.get("title") and query.lower() in paper["title"].lower():
        return True
    if paper.get("source_tier", 0) >= 3:  # found in tier-3 source alone is enough
        return True
    return False


def _should_drop_low_quality(paper: dict[str, Any], query: str = "") -> bool:
    """Safe low-quality filter. Returns True if paper should be dropped.
    Rescued papers always pass (high citations, multi-source, survey, exact title match)."""
    if _is_rescued(paper, query):
        return False
    return (
        not paper.get("abstract")
        and _to_int(paper.get("citation_count")) < 5
        and _to_int(paper.get("source_count")) <= 1
        and _to_int(paper.get("source_tier")) < 2
    )


def _merge_papers(
    items: list[dict[str, Any]],
    limit: int,
    query: str | None = None,
    mode: str = "comprehensive",
    field: str = "general",
    debug: bool = False,
) -> list[dict[str, Any]]:
    """Deduplicate, score, and rank papers.

    Relevance score (0-10) = 0.7 * semantic (cosine via bge-small) + 0.3 * keyword_overlap
                            + citation boost (0-3) + survey boost (+1) + author boost (0-1)
    Mode filters/reranks the result set:
      - "seminal":        sort by citation_count desc, year asc; require >=10 citations
      - "recent":         keep last 2 years; sort by citation_count desc
      - "survey":         keep review/survey/meta-analysis papers only
      - "comprehensive":  default behavior
    Field is used for source-tier weighting (biomedical sources only count for medical/bio).
    When debug=True, each paper includes a score_breakdown dict.
    """
    # Mode-specific filter (apply before scoring to skip wasted work)
    if mode == "survey":
        items = [p for p in items if p.get("is_survey")]
    elif mode == "recent":
        from datetime import datetime
        cutoff = datetime.now().year - 2
        items = [p for p in items if (_to_int(p.get("year")) or 0) >= cutoff]
    elif mode == "seminal":
        items = [p for p in items if _to_int(p.get("citation_count")) >= 10]

    # Extract query terms for keyword scoring
    query_terms: set[str] = set()
    if query:
        q_clean = re.sub(r"[^\w\s]", "", query.casefold())
        query_terms = {w for w in q_clean.split() if len(w) > 2}

    # Pre-compute query embedding once (None if embedder unavailable)
    q_vec = _cached_embed(query) if query else None

    merged: dict[str, dict[str, Any]] = {}
    for item in items:
        key = _paper_key(item)
        if not key or key.startswith("title::"):
            continue
        existing = merged.get(key)
        if existing is None:
            sources = item.get("sources") or []
            raw_hits = len(set(sources))
            precision_boost = 0
            for s in sources:
                precision_boost = max(precision_boost, SOURCE_PRECISION_BONUS.get(s, 0))
            item["source_hits"] = raw_hits + precision_boost
            # Source tier: max tier of any source, field-aware (tier-1 biomedical only counts for medical/bio)
            item["source_tier"] = max((_get_source_tier(s, field) for s in sources), default=0)

            # Relevance scoring: blend semantic + keyword
            keyword_score = 0.0
            if query_terms and item.get("title"):
                title_clean = re.sub(r"[^\w\s]", "", item["title"].casefold())
                title_terms = set(title_clean.split())
                keyword_score = min(len(title_terms & query_terms) * 3.0 / 10.0, 1.0)
            sem_score = 0.0
            if q_vec is not None:
                p_vec = _cached_embed(_passage_text(item))
                if p_vec:
                    sem_score = max(_cosine(q_vec, p_vec), 0.0)
            base = 0.7 * sem_score + 0.3 * keyword_score  # 0..1
            score = int(round(base * 10))  # 0..10

            # Citation boost
            cites = _to_int(item.get("citation_count"))
            cite_boost = 0
            if cites >= 500:
                cite_boost = 3
            elif cites >= 100:
                cite_boost = 2
            elif cites >= 50:
                cite_boost = 1
            score = min(score + cite_boost, 10)
            # Survey boost
            survey_boost = 1 if item.get("is_survey") else 0
            if survey_boost:
                score = min(score + 1, 10)
            # Author reputation boost
            h = _to_int(item.get("first_author_h"))
            author_boost = 1 if h >= 50 else 0
            if author_boost:
                score = min(score + 1, 10)
            item["relevance_score"] = score

            if debug:
                item["score_breakdown"] = {
                    "semantic": round(sem_score, 3),
                    "keyword": round(keyword_score, 3),
                    "source_tier": item["source_tier"],
                    "source_hits": item["source_hits"],
                    "citation_boost": cite_boost,
                    "survey_boost": survey_boost,
                    "author_boost": author_boost,
                    "final": score,
                }
            else:
                # Strip if a previous debug call mutated this input dict
                item.pop("score_breakdown", None)
            merged[key] = item
            continue
        new_sources = set(existing.get("sources") or []) | set(item.get("sources") or [])
        existing["sources"] = sorted(new_sources)
        raw_hits = len(new_sources)
        precision_boost = 0
        for s in new_sources:
            precision_boost = max(precision_boost, SOURCE_PRECISION_BONUS.get(s, 0))
        existing["source_hits"] = raw_hits + precision_boost
        existing["source_tier"] = max(
            _to_int(existing.get("source_tier")),
            max((_get_source_tier(s, field) for s in new_sources), default=0),
        )
        for f in ("abstract", "doi", "arxiv_id", "pmid", "paper_id", "url", "pdf_url", "venue", "year", "keywords"):
            if not existing.get(f) and item.get(f):
                existing[f] = item[f]
        if not existing.get("authors") and item.get("authors"):
            existing["authors"] = item["authors"]
        if not existing.get("is_survey") and item.get("is_survey"):
            existing["is_survey"] = True
        existing["citation_count"] = max(
            _to_int(existing.get("citation_count")), _to_int(item.get("citation_count"))
        )
        existing["relevance_score"] = max(
            _to_int(existing.get("relevance_score")),
            _to_int(item.get("relevance_score")),
        )
        existing["first_author_h"] = max(
            _to_int(existing.get("first_author_h")),
            _to_int(item.get("first_author_h")),
        )

    # Mode-specific ranking
    if mode == "seminal":
        # citations desc, then oldest year first
        sort_key = lambda p: (
            min(_to_int(p.get("citation_count")), 5000),
            -(_to_int(p.get("year")) or 9999),
        )
        ranked = sorted(merged.values(), key=sort_key, reverse=True)
    elif mode == "recent":
        # citations desc, then newest year first
        sort_key = lambda p: (
            min(_to_int(p.get("citation_count")), 5000),
            _to_int(p.get("year")) or 0,
        )
        ranked = sorted(merged.values(), key=sort_key, reverse=True)
    elif mode == "survey":
        # relevance desc, surveys first, then citations desc
        sort_key = lambda p: (
            _to_int(p.get("relevance_score")),
            1 if p.get("is_survey") else 0,
            min(_to_int(p.get("citation_count")), 5000),
        )
        ranked = sorted(merged.values(), key=sort_key, reverse=True)
    else:  # comprehensive
        # source_tier first, then source_hits, then relevance, then abstract, then cites, then year
        ranked = sorted(
            merged.values(),
            key=lambda p: (
                _to_int(p.get("source_tier")),
                _to_int(p.get("source_hits")),
                _to_int(p.get("relevance_score")),
                1 if p.get("abstract") else 0,
                min(_to_int(p.get("citation_count")), 5000),
                _to_int(p.get("year")),
            ),
            reverse=True,
        )

    # Safe low-quality filter (uses rescue rules: high-cite, multi-source, survey, exact title match, tier-3)
    ranked = [p for p in ranked if not _should_drop_low_quality(p, query or "")]

    # Only apply relevance-score floor when a query is given (walk_citations passes None)
    if query is not None and mode == "comprehensive":
        ranked = [p for p in ranked if _to_int(p.get("relevance_score")) >= 1]

    return ranked[:limit]


BEST_SOURCES = "arxiv,semantic,crossref,openalex,pmc,europepmc,openaire,doaj,unpaywall"


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
    arxiv_id = paper.get("arxiv_id") or paper.get("arxiv") or ""
    if arxiv_id and ARXIV_ID_RE.match(arxiv_id):
        return "arxiv"
    doi = str(paper.get("doi") or "").lower()
    if "biorxiv" in doi:
        return "biorxiv"
    if "medrxiv" in doi:
        return "medrxiv"
    if doi and DOI_RE.match(doi):
        # Valid DOI but no bio/medrxiv prefix; let downstream OA fallbacks handle it
        return ""
    pmid = str(paper.get("pmid") or "")
    if pmid and PMID_RE.match(pmid):
        return "pmc"  # PMC has actual full text
    source_list = paper.get("sources") or []
    for s in source_list:
        if s in ("arxiv", "semantic", "biorxiv", "medrxiv", "iacr", "openaire",
                 "citeseerx", "doaj", "base", "zenodo", "hal", "pmc", "europepmc"):
            return s
    return ""


async def _resolve_oa_id(paper_id: str) -> str:
    """Resolve any paper ID (DOI, arXiv, OpenAlex W...) to OpenAlex work ID."""
    if not paper_id:
        return ""
    # Already an OpenAlex W... ID
    if paper_id.startswith("W"):
        return paper_id
    try:
        client = await _get_client()
        oa_email = UNPAYWALL_EMAIL

        # Try DOI lookup first
        if paper_id.startswith("10.") or paper_id.startswith("doi:"):
            doi = paper_id.removeprefix("doi:")
            resp = await client.get(
                f"https://api.openalex.org/works/doi:{doi}",
                params={"mailto": oa_email},
            )
            if resp.status_code == 200:
                return (resp.json().get("id") or "").split("/")[-1]

        # Try arXiv ID — handle both "arxiv:1706.03762" and "10.48550/arXiv.1706.03762"
        arxiv = ""
        if paper_id.startswith("arxiv:") or paper_id.startswith("arXiv:"):
            arxiv = paper_id.split(":", 1)[1]
        elif "arxiv" in paper_id.lower():
            arxiv = paper_id.split("/")[-1]
        if arxiv:
            resp = await client.get(
                "https://api.openalex.org/works",
                params={"filter": f"ids:arxiv:{arxiv}", "per_page": 1, "mailto": oa_email},
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                if results:
                    return (results[0].get("id") or "").split("/")[-1]

        # Fallback: search by title/ID
        resp = await client.get(
            "https://api.openalex.org/works",
            params={"search": paper_id, "per_page": 1, "mailto": oa_email},
        )
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            if results:
                return (results[0].get("id") or "").split("/")[-1]

        return ""
    except Exception:
        return ""


async def _search_openalex_direct(query: str, max_results: int = 50, year_from: int | None = None, year_to: int | None = None) -> list[dict[str, Any]]:
    """Direct OpenAlex API search with full metadata. Returns normalized papers."""
    try:
        client = await _get_client()
        oa_email = UNPAYWALL_EMAIL
        filters = []
        if year_from:
            filters.append(f"from_publication_date:{year_from}-01-01")
        if year_to:
            filters.append(f"to_publication_date:{year_to}-12-31")
        params: dict[str, Any] = {
            "search": query,
            "per_page": min(max_results, 200),
            "mailto": oa_email,
        }
        if filters:
            params["filter"] = ",".join(filters)
        resp = await client.get("https://api.openalex.org/works", params=params)
        if resp.status_code != 200:
            return []
        data = resp.json()
        papers = [_parse_oa_work(r, include_pdf=True) for r in data.get("results", [])[:max_results]]
        return [_normalize_paper(p, "openalex-direct") for p in papers]
    except Exception:
        return []


def _parse_oa_work(r: dict[str, Any], include_pdf: bool = True) -> dict[str, Any]:
    """Parse a single OpenAlex work into our normalized paper format."""
    authors = []
    for a in (r.get("authorships") or [])[:8]:
        name = (a.get("author") or {}).get("display_name", "")
        if name:
            authors.append(name)
    abstract = ""
    abstract_inv = r.get("abstract_inverted_index")
    if abstract_inv:
        positions = []
        for word, pos_list in abstract_inv.items():
            for pos in pos_list:
                positions.append((pos, word))
        positions.sort()
        abstract = " ".join(w for _, w in positions)
    paper = {
        "title": r.get("title", ""),
        "authors": authors,
        "year": (r.get("publication_date") or "")[:4] or None,
        "doi": (r.get("doi") or "").removeprefix("https://doi.org/"),
        "abstract": abstract,
        "citation_count": _to_int(r.get("cited_by_count")),
        "url": r.get("id", ""),
    }
    if include_pdf:
        oa_info = r.get("open_access", {}) or {}
        paper["pdf_url"] = oa_info.get("oa_url") or (r.get("primary_location") or {}).get("pdf_url")
        paper["is_open_access"] = bool(oa_info.get("is_oa"))
    return paper


async def _openalex_citations(doi: str, direction: str = "cited_by", limit: int = 10) -> list[dict[str, Any]]:
    """Fetch forward (cited_by) or backward (references) citations via OpenAlex."""
    oa_id = await _resolve_oa_id(doi)
    if not oa_id:
        return []
    return await _fetch_oa_by_id(oa_id, direction, limit)


async def _fetch_oa_by_id(oa_id: str, direction: str, limit: int) -> list[dict[str, Any]]:
    """Fetch citations from OpenAlex using a resolved W... work ID."""
    try:
        client = await _get_client()
        oa_email = UNPAYWALL_EMAIL

        if direction == "cited_by":
            resp = await client.get(
                "https://api.openalex.org/works",
                params={"filter": f"cites:{oa_id}", "per_page": limit, "mailto": oa_email},
            )
        else:
            resp = await client.get(
                f"https://api.openalex.org/works/{oa_id}",
                params={"mailto": oa_email},
            )
            if resp.status_code != 200:
                return []
            refs = (resp.json() or {}).get("referenced_works", [])
            if not refs:
                return []
            ids_param = "|".join(refs[:limit])
            resp = await client.get(
                "https://api.openalex.org/works",
                params={"filter": f"ids:{ids_param}", "per_page": limit, "mailto": oa_email},
            )
        if resp.status_code != 200:
            return []
        results = resp.json().get("results", [])
        papers = [_parse_oa_work(r, include_pdf=False) for r in results[:limit]]
        return [_normalize_paper(p, f"openalex-{direction}") for p in papers]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Core tools
# ---------------------------------------------------------------------------

@mcp.tool()
def ping() -> dict[str, Any]:
    """Cheap health check. Use this FIRST to verify the MCP is reachable
    before doing expensive searches. Returns server status, configured API
    keys, and cache state. No network calls, completes in <5ms.

    Use the `status` field: "ok" = MCP is healthy, "degraded" = some
    backends are down but MCP itself works, "no-keys" = no API keys
    configured (results will be limited).
    """
    has_scopus = bool(os.environ.get("ELSEVIER_API_KEY"))
    has_springer = bool(os.environ.get("SPRINGER_API_KEY"))
    has_s2 = bool(_S2_API_KEY)
    s2_in_cooldown = time.monotonic() < _S2_QUOTA_UNTIL
    return {
        "status": "ok" if (has_s2 or has_scopus or has_springer) else "no-keys",
        "server": "research-mcp",
        "version": "lean-v6-browser",
        "tools": ["search_literature", "walk_citations", "read_paper", "browser_download", "ping"],
        "api_keys": {
            "semantic_scholar": has_s2,
            "scopus": has_scopus,
            "springer": has_springer,
            "unpaywall_email": UNPAYWALL_EMAIL != "research@example.com",
        },
        "s2_429_cooldown": s2_in_cooldown,
        "s2_cooldown_remaining_s": round(max(0, _S2_QUOTA_UNTIL - time.monotonic()), 1) if s2_in_cooldown else 0,
        "cache": {
            "search_entries": len(_search_cache._store),
            "lookup_entries": len(_lookup_cache._store),
            "embed_entries": len(_embed_cache),
            "author_hindex_cached": len(_author_hindex),
        },
        "browser_download_policy": {
            "max_parallel": 1,
            "human_delay_default": True,
            "ledger_file": "<save_path>/download_ledger.jsonl",
            "profile_path": str(Path.home() / ".cache" / "research-mcp" / "browser-profile"),
            "verification": "PDF text must match requested title keywords",
        },
        "fastembed_available": _get_embedder() is not None,
    }


@mcp.tool()
async def search_literature(
    query: str,
    max_results: int = 25,
    year_from: int | None = None,
    year_to: int | None = None,
    expand_queries: bool = True,
    mode: Literal["seminal", "recent", "survey", "comprehensive"] = "comprehensive",
    field: Literal["auto", "cs", "bio", "medical", "social", "general"] = "auto",
    debug: bool = False,
) -> dict[str, Any]:
    """Search academic papers across 8 sources (arXiv, Semantic Scholar, OpenAlex, CrossRef, Unpaywall, OpenAIRE, Scopus, Springer). Returns deduplicated, semantically ranked results with abstracts and citation counts.

    mode:
      - "comprehensive" (default): breadth-first
      - "seminal":       highly-cited foundational works (citations desc, oldest first)
      - "recent":        last 2 years, ranked by citations
      - "survey":        review/survey/meta-analysis papers only

    field: "auto" detects from query (cs/medical/bio/social/general). Used for source-tier
      weighting — biomedical tier-1 sources only count for medical/bio queries.

    debug: when True, each paper includes a 'score_breakdown' dict (semantic, keyword,
      source_tier, citation_boost, survey_boost, author_boost, final). Off by default.
    """
    # Detect field if auto
    if field == "auto":
        field = _detect_field(query)

    # Check cache (mode + field are part of the key)
    cache_key = (query, max_results, year_from, year_to, expand_queries, mode, field)
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
    has_scopus = bool(os.environ.get("ELSEVIER_API_KEY"))
    has_springer = bool(os.environ.get("SPRINGER_API_KEY"))
    tasks_per_query = 2 + int(has_scopus) + int(has_springer)

    # Per-task: (coro, source_label)
    task_specs: list[tuple[Any, str]] = []
    for q in queries:
        task_specs.append((academix_server.academic_search_papers(
            query=q,
            year_from=year_from,
            year_to=year_to,
            sort="relevance",
            limit=min(max(max_results, 1), 100),
            response_format="json",
        ), "academix"))
        task_specs.append((paper_search.search_papers(
            query=q,
            max_results_per_source=max(10, min(max_results, 50)),
            sources=BEST_SOURCES,
            year=year,
        ), "paper-search"))
        if has_scopus:
            task_specs.append((search_scopus(q, max_results=max_results, year_from=str(year_from) if year_from else None, year_to=str(year_to) if year_to else None), "scopus"))
        if has_springer:
            task_specs.append((search_springer(q, max_results=max_results, year_from=str(year_from) if year_from else None, year_to=str(year_to) if year_to else None), "springer"))

    # Per-source timeouts: one slow backend cannot block the others.
    # We wrap each task in its own wait_for so a hung source is killed
    # individually rather than failing the whole gather.
    PER_SOURCE_TIMEOUT = 12.0

    async def _guarded(coro):
        try:
            return await asyncio.wait_for(coro, timeout=PER_SOURCE_TIMEOUT)
        except asyncio.TimeoutError:
            return Exception(f"per-source timeout after {PER_SOURCE_TIMEOUT}s")
        except Exception as e:
            return e

    guarded_tasks = [(_guarded(coro), label) for coro, label in task_specs]
    outputs = await asyncio.gather(*[g[0] for g in guarded_tasks], return_exceptions=True)

    papers: list[dict[str, Any]] = []
    errors: dict[str, str] = {}

    def _extract_papers(out: Any, source: str) -> list[dict[str, Any]]:
        """Extract papers from a backend response, tagged with source name."""
        data = _json_load(out)
        raw_papers = data.get("papers", []) if isinstance(data, dict) else (out if isinstance(out, list) else [])
        return [_normalize_paper(p, source) for p in raw_papers]

    # Use modular arithmetic on iteration index (works regardless of which tasks errored)
    for i, (out, source) in enumerate(zip(outputs, [g[1] for g in guarded_tasks])):
        q_idx = min(i // tasks_per_query, len(queries) - 1)
        if isinstance(out, Exception):
            errors[f"{source}_{queries[q_idx]}"] = str(out)
        else:
            papers.extend(_extract_papers(out, source))

    # Author reputation boost (8s cap, handled inside _boost_authors)
    await _boost_authors(papers)

    merged = _merge_papers(papers, max_results, query, mode=mode, field=field, debug=debug)

    # Direct OpenAlex search (separate from paper_search wrapper for full metadata)
    try:
        oa_papers = await asyncio.wait_for(
            _search_openalex_direct(query, max_results=max_results, year_from=year_from, year_to=year_to),
            timeout=10.0,
        )
        if oa_papers:
            await _boost_authors(oa_papers)
            all_with_oa = merged + oa_papers
            merged = _merge_papers(all_with_oa, max_results, query, mode=mode, field=field, debug=debug)
    except asyncio.TimeoutError:
        errors["openalex-direct"] = "openalex-direct timeout after 10s"

    # Status field: ok if we got results, degraded if partial, failed if nothing
    n_ok_sources = sum(1 for k in errors if not k.startswith("openalex-direct"))
    if merged:
        status = "ok" if not errors else ("degraded" if n_ok_sources < tasks_per_query else "ok")
    else:
        status = "failed"

    result = {
        "query": query,
        "mode": mode,
        "field": field,
        "status": status,
        "queries_used": queries,
        "total_before_dedupe": len(papers),
        "returned": len(merged),
        "errors": errors,
        "papers": merged,
    }

    _search_cache.set(result, *cache_key)
    return result


@mcp.tool()
async def walk_citations(
    paper_id: str,
    direction: Literal["forward", "backward", "both"] = "forward",
    depth: int = 1,
    max_papers_per_hop: int = 10,
    max_total: int = 200,
) -> dict[str, Any]:
    """Follow citation graphs forward (who cites) or backward (what it cites), multi-hop. Uses OpenAlex (highest success rate). Only walks most-cited papers for deeper hops."""
    visited: set[str] = set()
    all_papers: list[dict[str, Any]] = []
    queue: deque[tuple[str, int]] = deque([(paper_id, 0)])
    visited.add(paper_id)
    truncated = False

    while queue:
        if len(all_papers) >= max_total:
            truncated = True
            break
        current_id, current_depth = queue.popleft()
        if current_depth >= depth:
            continue

        # Resolve any paper ID to OpenAlex work ID
        oa_id = await _resolve_oa_id(current_id)
        if not oa_id:
            continue

        # Fetch forward + backward from OpenAlex in parallel
        cite_tasks = []
        if direction in ("forward", "both"):
            cite_tasks.append(_fetch_oa_by_id(oa_id, "cited_by", max_papers_per_hop))
        if direction in ("backward", "both"):
            cite_tasks.append(_fetch_oa_by_id(oa_id, "references", max_papers_per_hop))

        try:
            cite_results = await asyncio.wait_for(
                asyncio.gather(*cite_tasks, return_exceptions=True),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            continue

        for r in cite_results:
            if isinstance(r, Exception) or not isinstance(r, list):
                continue
            for paper in r[:max_papers_per_hop]:
                if not isinstance(paper, dict):
                    continue
                if len(all_papers) >= max_total:
                    truncated = True
                    break
                pid = paper.get("doi") or paper.get("arxiv_id") or paper.get("paper_id") or ""
                if not pid or pid in visited:
                    continue
                visited.add(pid)
                paper["hop"] = current_depth + 1
                paper["via"] = current_id
                all_papers.append(paper)
                # Only queue highly-cited papers for deeper hops
                if current_depth + 1 < depth and _to_int(paper.get("citation_count")) >= 10:
                    queue.append((pid, current_depth + 1))
            if truncated:
                break
        if truncated:
            break

    deduped = _merge_papers(all_papers, len(all_papers))
    return {
        "root_paper": paper_id,
        "truncated": truncated,
        "max_total": max_total,
        "direction": direction,
        "depth": depth,
        "total_found": len(deduped),
        "papers": deduped,
    }


@mcp.tool()
async def browser_download(
    doi: str = "",
    url: str = "",
    title: str = "",
    save_path: str = "./downloads",
    ezproxy: str = "https://ezproxy.lb.polyu.edu.hk",
    profile_path: str = "",
    timeout_seconds: int = 180,
    headless: bool = False,
    use_ezproxy: bool = True,
    reuse_existing: bool = True,
    force: bool = False,
    human_delay: bool = True,
) -> dict[str, Any]:
    """Download a paywalled paper through a real browser session.

    Uses Playwright with a persistent browser profile so institutional SSO cookies
    survive across calls. First run may open a visible browser and require manual
    PolyU login/2FA. Later runs reuse the saved EZproxy/SAML cookies.

    This tool verifies the extracted PDF text against the requested title. It will
    delete mismatched PDFs and return success=False rather than accepting a wrong
    repository fallback as a valid paper.
    """
    global _last_browser_request_at
    candidates = _browser_candidate_urls(doi=doi, url=url)
    if not candidates:
        return {
            "success": False,
            "error": "browser_download requires doi or url",
            "doi": doi,
            "url": url,
        }

    # arXiv papers are open-access; browser_download is the wrong tool and will
    # time out. Route to read_paper early with a clear hint.
    doi_lc = (doi or "").strip().casefold()
    if doi_lc.startswith("arxiv:") or "arxiv.org" in (url or "").casefold():
        return {
            "success": False,
            "status": "open_access_redirect",
            "error": (
                "arXiv papers are open-access and do not need a browser. "
                "Call read_paper(doi='arXiv:NNNN.NNNNN') instead."
            ),
            "doi": doi,
            "url": url,
            "suggested_tool": "read_paper",
        }

    out_dir = Path(save_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(
        profile_path
        or os.environ.get("RESEARCH_MCP_BROWSER_PROFILE", "")
        or (Path.home() / ".cache" / "research-mcp" / "browser-profile")
    )
    profile_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "success": False,
        "doi": doi,
        "title": title,
        "key": _paper_download_key(doi=doi, title=title, url=url),
        "save_path": str(out_dir),
        "profile_path": str(profile_dir),
        "ezproxy": ezproxy,
        "headless": headless,
        "reuse_existing": reuse_existing,
        "force": force,
        "human_delay": human_delay,
        "attempts": [],
        "candidates": candidates,
    }

    key = result["key"]
    if reuse_existing and not force:
        cached = _find_verified_ledger_hit(str(out_dir), key, title)
        if cached:
            return {**result, **cached, "success": True, "status": "reused"}
        blocked = _recent_retry_block(str(out_dir), key)
        if blocked:
            return {
                **result,
                "success": False,
                "status": "retry_blocked",
                "error": "recent failed attempt is still in cooldown; use force=True to override",
                "last_failure": blocked,
            }

    try:
        from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
    except Exception as exc:
        result["error"] = f"playwright is not installed or not importable: {exc}"
        return result

    timeout_ms = max(30, min(timeout_seconds, 600)) * 1000
    deadline = time.monotonic() + max(30, min(timeout_seconds, 600))

    async def _try_save_pdf(body: bytes, source_url: str, source: str) -> dict[str, Any] | None:
        if not body or body[:5] != PDF_MAGIC:
            return None
        paper_id = doi or title or "browser_download"
        saved = _save_pdf(body, paper_id, str(out_dir))
        pdf_result = _pdf_result_from_path(saved, title)
        if pdf_result["verified"]:
            return {
                "success": True,
                "doi": doi,
                "title": title,
                "key": key,
                "download_path": saved,
                "source": source,
                "source_url": source_url,
                **pdf_result,
            }
        try:
            Path(saved).unlink(missing_ok=True)
        except Exception:
            pass
        result["attempts"].append({
            "url": source_url,
            "source": source,
            "status": "pdf-title-mismatch-or-empty-text",
            "verification": pdf_result.get("verification"),
            "text_length": pdf_result.get("text_length", 0),
        })
        return None

    def _record_success(saved_result: dict[str, Any]) -> dict[str, Any]:
        record = {
            "key": key,
            "doi": doi,
            "title": title,
            "status": "success",
            "download_path": saved_result.get("download_path"),
            "source": saved_result.get("source"),
            "source_url": saved_result.get("source_url"),
            "text_length": saved_result.get("text_length"),
            "verification": saved_result.get("verification"),
        }
        _append_download_ledger(str(out_dir), record)
        saved_result["ledger_path"] = str(_ledger_path(str(out_dir)))
        saved_result["status"] = "success"
        return saved_result

    def _record_failure(status: str, error: str = "") -> None:
        _append_download_ledger(str(out_dir), {
            "key": key,
            "doi": doi,
            "title": title,
            "status": status,
            "error": error or result.get("error"),
            "retry_after_epoch": _retry_after_for_status(status),
            "attempt_count": len(result.get("attempts", [])),
            "attempts_tail": result.get("attempts", [])[-5:],
        })

    async def _wait_for_possible_sso(page) -> None:
        """If an SSO/login page is visible, give the user time to finish it."""
        while time.monotonic() < deadline:
            try:
                page_url = page.url.lower()
                title_text = (await page.title()).lower()
                content = (await page.content())[:5000].lower()
            except Exception:
                await asyncio.sleep(1)
                continue
            looks_like_login = (
                "idp.polyu.edu.hk" in page_url
                or "shibboleth authentication request" in content
                or "sign in" in title_text
                or "login" in title_text
                or "password" in content
            )
            if not looks_like_login:
                return
            result["needs_login"] = True
            await asyncio.sleep(2)

    async def _collect_links(page) -> list[str]:
        try:
            links = await page.eval_on_selector_all(
                "a",
                """els => els.map(a => ({href: a.href || '', text: (a.innerText || a.textContent || '').trim()}))""",
            )
        except Exception:
            return []
        out: list[str] = []
        for item in links:
            href = str(item.get("href") or "")
            text = str(item.get("text") or "").lower()
            if not href:
                continue
            hlow = href.lower()
            if (
                ".pdf" in hlow
                or "/doi/pdf" in hlow
                or "download" in hlow
                or "fulltext" in hlow
                or text in ("pdf", "download pdf", "full text")
                or "pdf" in text
            ):
                out.append(urljoin(page.url, href))
        return _dedupe_preserve_order(out)

    await _browser_download_lock.acquire()
    result["queued"] = True
    result["queue_policy"] = "single browser_download at a time"
    publisher = _publisher_from_url(candidates[0] if candidates else url)
    result["publisher"] = publisher
    if human_delay and _last_browser_request_at > 0:
        target_wait = _human_delay_seconds(publisher)
        elapsed = time.monotonic() - _last_browser_request_at
        wait_s = max(0.0, target_wait - elapsed)
        if wait_s > 0:
            result["human_delay_seconds"] = round(wait_s, 1)
            await asyncio.sleep(wait_s)
    _last_browser_request_at = time.monotonic()

    async with async_playwright() as p:
        launch_kwargs = {
            "headless": headless,
            "accept_downloads": True,
            "downloads_path": str(out_dir),
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        try:
            context = await p.chromium.launch_persistent_context(
                str(profile_dir), channel="chrome", **launch_kwargs
            )
        except Exception:
            try:
                context = await p.chromium.launch_persistent_context(str(profile_dir), **launch_kwargs)
            except Exception as exc:
                result["error"] = f"could not launch Playwright browser: {type(exc).__name__}: {str(exc)[:200]}"
                _record_failure("browser_error", result["error"])
                _browser_download_lock.release()
                return result

        try:
            page = context.pages[0] if context.pages else await context.new_page()
            page.set_default_timeout(timeout_ms)
            request_urls: list[str] = []

            # First visit pages in the browser. This is where SSO happens.
            for target in candidates:
                if time.monotonic() >= deadline:
                    break
                visit_urls = [_ezproxy_url(target, ezproxy)] if use_ezproxy else []
                visit_urls.append(target)
                for visit_url in _dedupe_preserve_order(visit_urls):
                    if time.monotonic() >= deadline:
                        break
                    try:
                        response = await page.goto(
                            visit_url,
                            wait_until="domcontentloaded",
                            timeout=max(5000, int((deadline - time.monotonic()) * 1000)),
                        )
                        await _wait_for_possible_sso(page)
                        if response is not None:
                            headers = {k.lower(): v for k, v in response.headers.items()}
                            ct = headers.get("content-type", "").lower()
                            result["attempts"].append({
                                "url": visit_url,
                                "status": response.status,
                                "content_type": ct,
                                "browser_url": page.url,
                            })
                            if response.status == 200 and "pdf" in ct:
                                saved_result = await _try_save_pdf(await response.body(), visit_url, "browser-page")
                                if saved_result:
                                    _browser_download_lock.release()
                                    return _record_success(saved_result)
                        request_urls.append(page.url)
                        request_urls.extend(await _collect_links(page))
                    except PlaywrightTimeoutError:
                        result["attempts"].append({"url": visit_url, "status": "timeout"})
                    except Exception as exc:
                        result["attempts"].append({"url": visit_url, "status": f"error:{type(exc).__name__}:{str(exc)[:120]}"})

            # Then use the authenticated browser context's request client to fetch PDF URLs.
            request_urls.extend(candidates)
            for target in _dedupe_preserve_order(request_urls):
                if time.monotonic() >= deadline:
                    break
                fetch_urls = [_ezproxy_url(target, ezproxy)] if use_ezproxy else []
                fetch_urls.append(target)
                for fetch_url in _dedupe_preserve_order(fetch_urls):
                    if time.monotonic() >= deadline:
                        break
                    try:
                        resp = await context.request.get(
                            fetch_url,
                            timeout=max(5000, int((deadline - time.monotonic()) * 1000)),
                            max_redirects=10,
                        )
                        ct = (resp.headers.get("content-type") or "").lower()
                        result["attempts"].append({
                            "url": fetch_url,
                            "status": resp.status,
                            "content_type": ct,
                            "source": "browser-request",
                        })
                        body = await resp.body()
                        if resp.status == 200 and ("pdf" in ct or body[:5] == PDF_MAGIC):
                            saved_result = await _try_save_pdf(body, fetch_url, "browser-request")
                            if saved_result:
                                _browser_download_lock.release()
                                return _record_success(saved_result)
                    except Exception as exc:
                        result["attempts"].append({"url": fetch_url, "status": f"request-error:{type(exc).__name__}:{str(exc)[:120]}"})
        finally:
            # Keep cookies/profile on disk, but close this browser process.
            try:
                await context.close()
            except Exception:
                pass

    has_mismatch = any(a.get("status") == "pdf-title-mismatch-or-empty-text" for a in result.get("attempts", []))
    failure_status = "title_mismatch" if has_mismatch else ("needs_login" if result.get("needs_login") else "browser_timeout")
    result["status"] = failure_status
    if failure_status == "title_mismatch":
        result["error"] = "downloaded PDF(s) did not match the requested paper title; mismatched files were deleted"
    elif failure_status == "needs_login":
        result["error"] = (
            "browser_download needs PolyU SSO. Finish login in the opened browser window; "
            "cookies will persist for future calls."
        )
    else:
        result["error"] = "browser_download could not get a verified PDF before timeout"
    _record_failure(failure_status, result["error"])
    if _browser_download_lock.locked():
        _browser_download_lock.release()
    return result


@mcp.tool()
async def read_paper(
    paper_id: str,
    source: str = "auto",
    doi: str = "",
    title: str = "",
    save_path: str = "./downloads",
    use_scihub: bool = False,
) -> dict[str, Any]:
    """Download and extract full text from a paper. Falls back through OA repositories, Unpaywall, optionally Sci-Hub.

    Capped at 30s total to avoid blocking the MCP client when many calls fire in parallel.
    Sci-Hub is OFF by default — it adds 15-45s per call and is rate-limited. Set use_scihub=True
    only when no OA copy is available and you accept the latency.
    """
    # Extract DOI from paper_id if not provided separately
    if not doi and DOI_RE.match(paper_id):
        doi = paper_id
    # Extract arXiv ID from paper_id for fallback
    arxiv_id = ""
    if ARXIV_ID_RE.match(paper_id):
        arxiv_id = paper_id
    elif paper_id.lower().startswith("arxiv:"):
        arxiv_id = paper_id.split(":", 1)[1]

    if source == "auto":
        # Only pass an ID field if the value actually matches its expected format
        # (e.g. a DOI like "10.1234/foo" must not be passed as arxiv_id).
        source = _detect_source_from_paper({
            "arxiv_id": arxiv_id,
            "doi": doi,
            "pmid": paper_id if PMID_RE.match(paper_id) else "",
        })

    result: dict[str, Any] = {"paper_id": paper_id, "source": source, "fallbacks_tried": []}
    overall_deadline = time.monotonic() + 35.0  # hard cap so 5 parallel calls finish in ~35s wall time

    reader = _get_reader(source)
    if reader:
        try:
            text = await asyncio.wait_for(
                reader(paper_id, save_path=save_path),
                timeout=max(1.0, overall_deadline - time.monotonic()),
            )
            if text:
                result["text"] = text
                result["text_length"] = len(text)
                result["verification"] = _verify_text_matches_title(text, title)
                result["success"] = result["verification"].get("match", True)
                if result["success"]:
                    result["oa_source"] = source
                    return result
                result["fallbacks_tried"].append(f"{source}:title-mismatch")
            result["reader_empty"] = True
        except asyncio.TimeoutError:
            result["reader_error"] = f"reader timeout after {overall_deadline - (time.monotonic() - 30.0):.1f}s"
        except Exception as exc:
            result["reader_error"] = str(exc)

    # Try paper_search.download_with_fallback (handles Unpaywall + multiple repositories)
    remaining = max(1.0, overall_deadline - time.monotonic())
    if remaining > 0.5:
        try:
            path = await asyncio.wait_for(
                paper_search.download_with_fallback(
                    source=source, paper_id=paper_id, doi=doi, title=title,
                    save_path=save_path, use_scihub=use_scihub,
                ),
                timeout=remaining,
            )
            # download_with_fallback returns either a real path string OR an error message string.
            # Treat error messages (starting with "Download failed") as failure.
            if path and not str(path).startswith("Download failed") and not str(path).startswith("Error"):
                result["download_path"] = path
                # Extract text from the downloaded PDF (this is what the user actually wants)
                pdf_result = _pdf_result_from_path(path, title)
                result.update(pdf_result)
                result["success"] = pdf_result["verified"]
                if result["success"]:
                    result["oa_source"] = "paper-search"
                    return result
                result["fallbacks_tried"].append("paper_search:empty-text-or-title-mismatch")
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception:
                    pass
            else:
                result["download_path"] = path  # keep the error string for debugging
                result["fallbacks_tried"].append("paper_search_fallback")
        except asyncio.TimeoutError:
            result["download_error"] = f"paper_search timeout after {remaining:.1f}s"
            result["success"] = False
        except Exception as exc:
            result["download_error"] = str(exc)
            result["fallbacks_tried"].append("paper_search_fallback")
            result["success"] = False

    # Additional OA fallbacks (if DOI provided and main download failed)
    if not result.get("success") and doi:
        client = await _get_client()

        # Try OpenAlex OA URL (has open_access.oa_url for many papers)
        try:
            remaining = max(1.0, overall_deadline - time.monotonic())
            if remaining > 0.5:
                oa_url = None
                resp = await asyncio.wait_for(
                    client.get(
                        f"https://api.openalex.org/works/doi:{doi}",
                        params={"mailto": UNPAYWALL_EMAIL},
                    ),
                    timeout=remaining,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    oa_info = data.get("open_access", {})
                    oa_url = oa_info.get("oa_url")
                    if not oa_url:
                        primary = data.get("primary_location", {})
                        if primary and primary.get("pdf_url"):
                            oa_url = primary["pdf_url"]
                result["fallbacks_tried"].append(f"openalex:{oa_url[:60] if oa_url else 'no-oa-url'}")
                if oa_url:
                    pdf_resp = await asyncio.wait_for(
                        client.get(oa_url, timeout=15.0),
                        timeout=max(1.0, overall_deadline - time.monotonic()),
                    )
                    if pdf_resp.status_code == 200 and _is_pdf_response(pdf_resp, pdf_resp.content):
                        saved_path = _save_pdf(pdf_resp.content, paper_id, save_path)
                        result["download_path"] = saved_path
                        pdf_result = _pdf_result_from_path(saved_path, title)
                        result.update(pdf_result)
                        if pdf_result["verified"]:
                            result["success"] = True
                            result["oa_source"] = "openalex"
                            return result
                        try:
                            Path(saved_path).unlink(missing_ok=True)
                        except Exception:
                            pass
        except (asyncio.TimeoutError, Exception):
            pass

        # Try Springer OA
        try:
            remaining = max(1.0, overall_deadline - time.monotonic())
            if remaining > 0.5:
                oa_url = await asyncio.wait_for(
                    springer_resolve_oa(doi),
                    timeout=remaining,
                )
                result["fallbacks_tried"].append(f"springer:{'ok' if oa_url else 'no-oa-url'}")
                if oa_url:
                    pdf_resp = await asyncio.wait_for(
                        client.get(oa_url, timeout=15.0),
                        timeout=max(1.0, overall_deadline - time.monotonic()),
                    )
                    if pdf_resp.status_code == 200 and _is_pdf_response(pdf_resp, pdf_resp.content):
                        saved_path = _save_pdf(pdf_resp.content, paper_id, save_path)
                        result["download_path"] = saved_path
                        pdf_result = _pdf_result_from_path(saved_path, title)
                        result.update(pdf_result)
                        if pdf_result["verified"]:
                            result["success"] = True
                            result["oa_source"] = "springer"
                            return result
                        try:
                            Path(saved_path).unlink(missing_ok=True)
                        except Exception:
                            pass
        except (asyncio.TimeoutError, Exception):
            pass

        # Try Sci-Hub (only if explicitly enabled — slow + rate-limited)
        if use_scihub and time.monotonic() < overall_deadline:
            env_mirrors = os.environ.get("SCI_HUB_MIRRORS", "").strip()
            sci_hub_urls = (
                [m.strip() for m in env_mirrors.split(",") if m.strip()]
                if env_mirrors
                else ["https://sci-hub.se"]
            )
            # Tight timeout per mirror so we don't waste the whole budget on one
            remaining = max(1.0, overall_deadline - time.monotonic())
            per_mirror = min(8.0, remaining / max(len(sci_hub_urls), 1))
            async with httpx.AsyncClient(follow_redirects=True, timeout=per_mirror) as sc:
                for mirror in sci_hub_urls:
                    if time.monotonic() >= overall_deadline:
                        break
                    try:
                        resp = await sc.get(f"{mirror}/{doi}")
                        result["fallbacks_tried"].append(f"scihub:{mirror}:{resp.status_code}")
                        if resp.status_code == 200 and _is_pdf_response(resp, resp.content):
                            saved_path = _save_pdf(resp.content, paper_id, save_path)
                            result["download_path"] = saved_path
                            pdf_result = _pdf_result_from_path(saved_path, title)
                            result.update(pdf_result)
                            if pdf_result["verified"]:
                                result["success"] = True
                                result["oa_source"] = f"scihub:{mirror}"
                                return result
                            try:
                                Path(saved_path).unlink(missing_ok=True)
                            except Exception:
                                pass
                    except Exception:
                        continue

    # If we get here, nothing worked
    if not result.get("success"):
        result["success"] = False
        result["error"] = (
            f"all fallbacks failed: tried {result.get('fallbacks_tried', [])}. "
            f"paper is likely paywalled with no OA copy. try a different source or arXiv preprint."
        )
    return result


# ---------------------------------------------------------------------------
# Curation tools
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
