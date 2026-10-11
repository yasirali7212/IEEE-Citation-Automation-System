"""
Zotero Web API Client
======================
Connects to the Zotero Web API to retrieve library items.
Credentials are read from environment variables - never hardcoded.

API docs: https://www.zotero.org/support/dev/web_api/v3/basics
"""

import os
import json
import logging
import time
from typing import Optional
import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

ZOTERO_BASE_URL = "https://api.zotero.org"
REQUEST_DELAY = 0.2  # seconds between requests (be polite)


class ZoteroClient:
    """Thin client for Zotero Web API v3."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        user_id: Optional[str] = None,
        library_type: str = "user",
    ):
        self.api_key = api_key or os.getenv("ZOTERO_API_KEY", "")
        self.user_id = user_id or os.getenv("ZOTERO_USER_ID", "")
        self.library_type = library_type or os.getenv("ZOTERO_LIBRARY_TYPE", "user")

        if not self.api_key:
            raise ValueError(
                "Zotero API key is missing. "
                "Set ZOTERO_API_KEY in your .env file."
            )

        # Auto-resolve numeric User ID if not provided or non-numeric (e.g. username)
        if not self.user_id or not str(self.user_id).isdigit():
            try:
                res = requests.get(
                    f"{ZOTERO_BASE_URL}/keys/{self.api_key}",
                    headers={"Zotero-API-Version": "3"},
                    timeout=10
                )
                if res.status_code == 200:
                    data = res.json()
                    self.user_id = str(data.get("userID", ""))
                    logger.info(f"Auto-resolved Zotero user ID: {self.user_id}")
            except Exception as e:
                logger.warning(f"Could not auto-resolve Zotero user ID: {e}")

        if not self.user_id:
            raise ValueError(
                "Zotero User ID is missing. "
                "Set ZOTERO_USER_ID in your .env file."
            )

        self.base_url = (
            f"{ZOTERO_BASE_URL}/{self.library_type}s/{self.user_id}"
        )
        self.session = requests.Session()
        # Both headers are REQUIRED by Zotero Web API v3:
        # https://www.zotero.org/support/dev/web_api/v3/basics
        self.session.headers.update({
            "Zotero-API-Key": self.api_key,
            "Zotero-API-Version": "3",   # mandatory per official docs
            "Accept": "application/json",
        })

    def _get(self, endpoint: str, params: dict = None) -> dict | list:
        """Make a GET request to the Zotero API."""
        url = f"{self.base_url}{endpoint}"
        try:
            resp = self.session.get(url, params=params or {}, timeout=15)
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return resp.json()
        except requests.RequestException as e:
            logger.error(f"Zotero API error for {url}: {e}")
            raise

    def get_all_items(self, limit: int = 100) -> list[dict]:
        """
        Retrieve all items from the library, handling pagination.
        Endpoint: GET /users/{userId}/items
        Max limit per request: 100 (official API cap)
        Returns a list of Zotero item data dicts.
        """
        items = []
        start = 0
        while True:
            batch = self._get("/items", params={
                "limit": min(limit, 100),   # API max is 100
                "start": start,
                "format": "json",
                "itemType": "-attachment",  # skip attachments, get refs only
            })
            if not batch:
                break
            items.extend(batch)
            if len(batch) < limit:
                break
            start += limit
            logger.info(f"Fetched {len(items)} Zotero items so far...")
        logger.info(f"Total Zotero items retrieved: {len(items)}")
        return items

    def search_by_title(self, title: str) -> list[dict]:
        """
        Search library for items matching a title string.
        Endpoint: GET /users/{userId}/items?q={query}
        The 'q' parameter searches across all fields (title, author, etc.)
        """
        return self._get("/items", params={
            "q": title,
            "qmode": "titleCreatorYear",  # search title, creator, year fields
            "format": "json",
            "limit": 10,
        })

    def get_item_by_key(self, key: str) -> dict:
        """Retrieve a single item by its Zotero key."""
        return self._get(f"/items/{key}")

    def normalize_item(self, item: dict) -> dict:
        """
        Flatten a Zotero item into a simple dict for downstream use.
        Returns: {title, authors, year, journal, doi, url, volume, issue, pages, type}
        """
        data = item.get("data", {})
        creators = data.get("creators", [])
        authors = []
        for c in creators:
            if c.get("creatorType") == "author":
                last = c.get("lastName", "")
                first = c.get("firstName", "")
                initials = ". ".join(w[0] for w in first.split() if w) + "." if first else ""
                authors.append(f"{last}, {initials}".strip(", "))

        return {
            "zotero_key": data.get("key", ""),
            "title": data.get("title", ""),
            "authors": authors,
            "year": data.get("date", "")[:4] if data.get("date") else "",
            "journal": data.get("publicationTitle", data.get("conferenceName", "")),
            "doi": data.get("DOI", ""),
            "url": data.get("url", ""),
            "volume": data.get("volume", ""),
            "issue": data.get("issue", ""),
            "pages": data.get("pages", ""),
            "publisher": data.get("publisher", ""),
            "item_type": data.get("itemType", ""),
            "abstract": data.get("abstractNote", ""),
        }

    def build_lookup_index(self) -> dict[str, dict]:
        """
        Build a title-based lookup index from the full library.
        Returns {lowercase_title: normalized_item}
        """
        items = self.get_all_items()
        index = {}
        for item in items:
            norm = self.normalize_item(item)
            key = norm["title"].lower().strip()
            if key:
                index[key] = norm
        logger.info(f"Zotero index built with {len(index)} entries.")
        return index


def save_zotero_library(output_path: str, client: Optional[ZoteroClient] = None) -> str:
    """
    Fetch the full Zotero library and save to a JSON file.
    Returns the path of the saved file.
    """
    if client is None:
        client = ZoteroClient()
    index = client.build_lookup_index()
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(index, f, indent=2, ensure_ascii=False)
    logger.info(f"Zotero library saved: {output_path}")
    return output_path


if __name__ == "__main__":
    import sys
    from pathlib import Path

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    output_dir = sys.argv[1] if len(sys.argv) > 1 else os.getenv("OUTPUT_FOLDER", "outputs")
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    out_path = str(Path(output_dir) / "zotero_library.json")

    try:
        client = ZoteroClient()
        save_zotero_library(out_path, client)
        print(f"Zotero library saved to: {out_path}")
    except ValueError as e:
        print(f"Configuration error: {e}")
        print("Please set ZOTERO_API_KEY and ZOTERO_USER_ID in your .env file.")
        sys.exit(1)
