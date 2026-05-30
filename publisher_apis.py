"""Publisher API integrations for research-mcp (Scopus, Springer).

Each function returns normalized paper dicts matching the bundle's format.
API keys read from environment variables.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any
from urllib.parse import quote

import httpx

SCOPUS_BASE = "https://api.elsevier.com/content/search/scopus"
SPRINGER_BASE = "https://api.springernature.com/metadata/json"
SPRINGER_OA_BASE = "https://api.springernature.com/openaccess/json"


# ---------------------------------------------------------------------------
# Scopus (Elsevier)
# ---------------------------------------------------------------------------

def _scopus_headers() -> dict[str, str]:
    api_key = os.environ.get("ELSEVIER_API_KEY", "")
    inst_token = os.environ.get("ELSEVIER_INST_TOKEN", "")
    headers = {
        "Accept": "application/json",
        "X-ELS-ResourceVersion": "XOCS",
    }
    if api_key:
        headers["X-ELS-APIKey"] = api_key
    if inst_token:
        headers["X-ELS-Insttoken"] = inst_token
    return headers


def _normalize_scopus(entry: dict[str, Any]) -> dict[str, Any]:
    """Convert a Scopus API entry to normalized paper format."""
    doi = entry.get("prism:doi", "")
    arxiv_id = ""
    scopus_id = entry.get("dc:identifier", "").replace("SCOPUS_ID:", "")

    title = entry.get("dc:title", "")
    if isinstance(title, list):
        title = title[0] if title else ""

    authors_raw = entry.get("dc:creator", "")
    authors = [a.strip() for a in re.split(r";|,", authors_raw) if a.strip()] if authors_raw else []

    date_str = entry.get("prism:coverDate", "")
    year = date_str[:4] if len(date_str) >= 4 else None

    pub_name = entry.get("prism:publicationName", "")
    if isinstance(pub_name, list):
        pub_name = pub_name[0] if pub_name else ""

    keywords = entry.get("authkeywords", "")
    if isinstance(keywords, str) and keywords:
        keywords = [k.strip() for k in keywords.split(";") if k.strip()]
    else:
        keywords = []

    try:
        citations = int(entry.get("citedby-count", 0))
    except (ValueError, TypeError):
        citations = 0

    links = entry.get("link", []) or []
    pdf_url = ""
    scopus_url = ""
    for link in links:
        if isinstance(link, dict):
            href = link.get("@href", "")
            if "@ref" in link:
                if link["@ref"] == "scopus":
                    scopus_url = href
                elif link["@ref"] == "full-text":
                    pdf_url = href

    return {
        "paper_id": scopus_id,
        "title": title,
        "authors": authors,
        "abstract": "",
        "year": year,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "source": "scopus",
        "citation_count": citations,
        "pdf_url": pdf_url,
        "url": scopus_url,
        "venue": pub_name,
        "keywords": keywords,
    }


async def search_scopus(
    query: str,
    max_results: int = 20,
    year_from: str | None = None,
    year_to: str | None = None,
) -> list[dict[str, Any]]:
    """Search Scopus via Elsevier API.

    Requires ELSEVIER_API_KEY env var. For full text, also set ELSEVIER_INST_TOKEN.
    """
    api_key = os.environ.get("ELSEVIER_API_KEY", "")
    if not api_key:
        return []

    # Build date filter
    date_filter = ""
    if year_from and year_to:
        date_filter = f" AND PUBYEAR AFT {int(year_from) - 1} AND PUBYEAR BEF {int(year_to) + 1}"
    elif year_from:
        date_filter = f" AND PUBYEAR AFT {int(year_from) - 1}"
    elif year_to:
        date_filter = f" AND PUBYEAR BEF {int(year_to) + 1}"

    full_query = f'TITLE-ABS-KEY({query}){date_filter}'
    count = min(max_results, 200)
    params: dict[str, Any] = {
        "query": full_query,
        "count": count,
        "sort": "relevance",
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(SCOPUS_BASE, headers=_scopus_headers(), params=params, timeout=30.0)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []

    entries = data.get("search-results", {}).get("entry", []) if isinstance(data, dict) else []
    return [_normalize_scopus(e) for e in entries[:max_results]]


# ---------------------------------------------------------------------------
# Springer Nature
# ---------------------------------------------------------------------------

def _normalize_springer(record: dict[str, Any]) -> dict[str, Any]:
    """Convert a Springer API record to normalized paper format."""
    doi = record.get("doi", "")
    title = record.get("title", "")
    if isinstance(title, list):
        title = title[0] if title else ""

    creators = record.get("creators", []) or []
    authors = []
    for c in creators:
        if isinstance(c, dict):
            name = c.get("creator", "")
            if name:
                authors.append(name)
        elif isinstance(c, str):
            authors.append(c)

    date_str = record.get("publicationDate", "") or record.get("coverDate", "")
    year = date_str[:4] if len(date_str) >= 4 else None

    abstract = record.get("abstract", "")
    if isinstance(abstract, list):
        abstract = abstract[0] if abstract else ""

    pub_name = record.get("publicationName", "")
    if isinstance(pub_name, list):
        pub_name = pub_name[0] if pub_name else ""

    keywords = record.get("keyword", []) or []
    if isinstance(keywords, str):
        keywords = [keywords]

    url = record.get("url", [])
    if isinstance(url, list):
        url = url[0] if url else {}
    pdf_url = url.get("value", "") if isinstance(url, dict) else ""
    record_url = record.get("url", "")
    if isinstance(record_url, list):
        record_url = record_url[0].get("value", "") if len(record_url) > 0 else ""

    is_oa = record.get("openaccess", "") == "true"
    genre = record.get("genre", "")

    return {
        "paper_id": doi or record.get("identifier", ""),
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "year": year,
        "doi": doi,
        "arxiv_id": "",
        "source": "springer",
        "citation_count": 0,
        "pdf_url": pdf_url if is_oa else "",
        "url": record_url,
        "venue": pub_name,
        "keywords": keywords,
        "is_open_access": is_oa,
        "genre": genre,
    }


async def search_springer(
    query: str,
    max_results: int = 20,
    year_from: str | None = None,
    year_to: str | None = None,
) -> list[dict[str, Any]]:
    """Search Springer Nature via Meta API.

    Requires SPRINGER_API_KEY env var.
    """
    api_key = os.environ.get("SPRINGER_API_KEY", "")
    if not api_key:
        return []

    year_filter = ""
    if year_from and year_to:
        year_filter = f" AND date:{year_from}-{year_to}"
    elif year_from:
        year_filter = f" AND date:{year_from}-"
    elif year_to:
        year_filter = f" AND date:-{year_to}"

    full_query = f"{query}{year_filter}"
    params: dict[str, Any] = {
        "q": full_query,
        "api_key": api_key,
        "p": min(max_results, 100),
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(SPRINGER_BASE, params=params, timeout=30.0)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return []

    records = data.get("records", []) if isinstance(data, dict) else []
    return [_normalize_springer(r) for r in records[:max_results]]


# ---------------------------------------------------------------------------
# Springer Open Access (resolve DOI → OA PDF link)
# ---------------------------------------------------------------------------

async def springer_resolve_oa(doi: str) -> str | None:
    """Resolve a DOI to an OA PDF URL via Springer Nature Open Access API.

    Returns the PDF URL if found, None otherwise.
    """
    api_key = os.environ.get("SPRINGER_API_KEY", "")
    if not api_key or not doi:
        return None

    params: dict[str, Any] = {
        "q": f"doi:{doi}",
        "api_key": api_key,
        "p": 1,
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(SPRINGER_OA_BASE, params=params, timeout=15.0)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None

    records = data.get("records", []) if isinstance(data, dict) else []
    for rec in records[:1]:
        urls = rec.get("url", [])
        if isinstance(urls, list):
            for u in urls:
                if isinstance(u, dict) and u.get("@format") == "pdf":
                    return u.get("value", "")
        oa_link = rec.get("openaccess", "")
        if oa_link and oa_link.startswith("http"):
            return oa_link
    return None
