"""
Crossref & DOI Metadata Lookup Client
=======================================
Looks up scholarly metadata for references using:
  - Crossref REST API (free, no key required)
  - DOI.org content negotiation
  - Semantic Scholar API (fallback, free)

RULE: Never invent metadata. If a source cannot be verified, flag it.
"""

import re
import time
import logging
import requests
from typing import Optional
from dotenv import load_dotenv
import os

load_dotenv()
logger = logging.getLogger(__name__)

CROSSREF_BASE = "https://api.crossref.org"
DOI_BASE = "https://doi.org"
SEMANTIC_SCHOLAR_BASE = "https://api.semanticscholar.org/graph/v1"
REQUEST_DELAY = 0.5  # polite delay between API calls
MAX_RETRIES = 3


def _get(url: str, params: dict = None, headers: dict = None,
         retries: int = MAX_RETRIES) -> Optional[dict]:
    """Shared GET with retry logic."""
    email = os.getenv("CROSSREF_EMAIL", "")
    h = {"User-Agent": f"IEEE-Citation-Workflow/1.0 (mailto:{email})"}
    if headers:
        h.update(headers)
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params or {}, headers=h, timeout=15)
            if resp.status_code == 429:  # rate limited
                wait = int(resp.headers.get("Retry-After", 10))
                logger.warning(f"Rate limited. Waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return resp.json()
        except requests.RequestException as e:
            logger.warning(f"Attempt {attempt}/{retries} failed for {url}: {e}")
            time.sleep(2 ** attempt)
    logger.error(f"All {retries} attempts failed for: {url}")
    return None


def lookup_by_doi(doi: str) -> Optional[dict]:
    """
    Resolve a DOI using Crossref metadata.
    Returns a normalized metadata dict or None if not found.
    """
    doi = doi.strip().lstrip("https://doi.org/").lstrip("http://dx.doi.org/")
    url = f"{CROSSREF_BASE}/works/{doi}"
    data = _get(url)
    if data and data.get("status") == "ok":
        return _normalize_crossref(data["message"])
    return None


def search_crossref(title: str, author: str = "", year: str = "") -> Optional[dict]:
    """
    Search Crossref for a publication by title (and optionally author/year).
    Returns the best-match normalized dict or None.
    """
    params = {
        "query.title": title,
        "rows": 3,
        "select": "DOI,title,author,published,container-title,volume,issue,page,type",
    }
    if author:
        params["query.author"] = author
    if year:
        params["filter"] = f"from-pub-date:{year},until-pub-date:{year}"

    data = _get(f"{CROSSREF_BASE}/works", params=params)
    if not data:
        return None

    items = data.get("message", {}).get("items", [])
    if not items:
        return None

    # Pick the best match by title similarity
    best = _pick_best_match(title, items)
    return _normalize_crossref(best) if best else None


def search_semantic_scholar(title: str) -> Optional[dict]:
    """Fallback: search Semantic Scholar for a paper by title."""
    params = {
        "query": title,
        "fields": "title,authors,year,externalIds,venue,volume,publicationVenueId",
        "limit": 3,
    }
    data = _get(f"{SEMANTIC_SCHOLAR_BASE}/paper/search", params=params)
    if not data:
        return None
    papers = data.get("data", [])
    if not papers:
        return None

    best = papers[0]  # Take the top result as a candidate
    doi = (best.get("externalIds") or {}).get("DOI", "")
    authors = [a.get("name", "") for a in (best.get("authors") or [])]

    return {
        "title": best.get("title", ""),
        "authors": authors,
        "year": str(best.get("year", "")),
        "journal": best.get("venue", ""),
        "doi": doi,
        "url": f"https://www.semanticscholar.org/paper/{best.get('paperId', '')}",
        "volume": "",
        "issue": "",
        "pages": "",
        "source": "semantic_scholar",
        "verified": True,
    }


def _normalize_crossref(item: dict) -> dict:
    """Convert a Crossref work dict into the standard normalized format."""
    # Authors
    raw_authors = item.get("author", [])
    authors = []
    for a in raw_authors:
        last = a.get("family", "")
        given = a.get("given", "")
        initials = ". ".join(w[0] for w in given.split() if w) + "." if given else ""
        if last:
            authors.append(f"{last}, {initials}".strip(", "))

    # Title
    title_list = item.get("title", [])
    title = title_list[0] if title_list else ""

    # Journal
    container = item.get("container-title", [])
    journal = container[0] if container else ""

    # Year
    pub = item.get("published", item.get("published-print", item.get("published-online", {})))
    date_parts = pub.get("date-parts", [[]])[0]
    year = str(date_parts[0]) if date_parts else ""

    doi = item.get("DOI", "")
    return {
        "title": title,
        "authors": authors,
        "year": year,
        "journal": journal,
        "doi": doi,
        "url": f"https://doi.org/{doi}" if doi else item.get("URL", ""),
        "volume": str(item.get("volume", "")),
        "issue": str(item.get("issue", "")),
        "pages": item.get("page", ""),
        "publisher": item.get("publisher", ""),
        "item_type": item.get("type", ""),
        "source": "crossref",
        "verified": True,
    }


def _pick_best_match(query_title: str, items: list[dict]) -> Optional[dict]:
    """
    Among Crossref results, return the one whose title most closely
    matches the query. Uses a simple token overlap heuristic.
    """
    query_tokens = set(re.sub(r'[^a-z0-9 ]', '', query_title.lower()).split())
    best_item = None
    best_score = 0

    for item in items:
        title_list = item.get("title", [])
        if not title_list:
            continue
        item_tokens = set(re.sub(r'[^a-z0-9 ]', '', title_list[0].lower()).split())
        if not item_tokens:
            continue
        overlap = len(query_tokens & item_tokens)
        score = overlap / max(len(query_tokens), len(item_tokens))
        if score > best_score:
            best_score = score
            best_item = item

    # Require at least 40% token overlap to accept
    if best_score >= 0.40:
        return best_item
    return None


def verify_reference(ref: dict) -> dict:
    """
    Given a reference dict (from extract_document.py), attempt to verify
    it against online sources. Returns the ref dict with added verification fields.
    """
    result = ref.copy()
    result["verified"] = False
    result["verification_source"] = ""
    result["verification_notes"] = ""
    result["verified_metadata"] = {}

    # Strategy 1: DOI lookup (most reliable)
    if ref.get("doi"):
        meta = lookup_by_doi(ref["doi"])
        if meta:
            result["verified"] = True
            result["verification_source"] = "crossref_doi"
            result["verified_metadata"] = meta
            logger.info(f"  [REF {ref['number']}] Verified via DOI: {ref['doi']}")
            return result

    # Strategy 2: Title search on Crossref
    raw_title = ref.get("title", "") or ref.get("raw_text", "")
    if raw_title and len(raw_title) > 15:
        meta = search_crossref(title=raw_title, year=ref.get("year", ""))
        if meta:
            result["verified"] = True
            result["verification_source"] = "crossref_title"
            result["verified_metadata"] = meta
            logger.info(f"  [REF {ref['number']}] Verified via Crossref title search.")
            return result

    # Strategy 3: Semantic Scholar fallback
    if raw_title and len(raw_title) > 15:
        meta = search_semantic_scholar(raw_title)
        if meta:
            result["verified"] = True
            result["verification_source"] = "semantic_scholar"
            result["verified_metadata"] = meta
            logger.info(f"  [REF {ref['number']}] Verified via Semantic Scholar.")
            return result

    # Could not verify
    result["verification_notes"] = (
        "Could not verify against Crossref or Semantic Scholar. "
        "Manual review required."
    )
    logger.warning(f"  [REF {ref['number']}] UNVERIFIED: {raw_title[:60]}")
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    # Quick test
    test_doi = "10.1109/TPDS.2020.3002456"
    print(f"\nLooking up DOI: {test_doi}")
    result = lookup_by_doi(test_doi)
    if result:
        print(f"  Title: {result['title']}")
        print(f"  Authors: {result['authors']}")
        print(f"  Year: {result['year']}")
    else:
        print("  Not found.")
