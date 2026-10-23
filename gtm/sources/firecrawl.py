"""Firecrawl v2 REST (optional fallback crawler for sites wigolo can't read, e.g. heavy JS).

POST /v2/map -> {"links": [{"url", "title"?}] | ["url"]}; POST /v2/scrape -> {"data": {"markdown",
"metadata"}}. One scrape = one credit. The key comes from the secrets file; Claude never sees it.
"""

from __future__ import annotations

import httpx

from gtm import secrets

BASE = "https://api.firecrawl.dev/v2"


class FirecrawlError(Exception):
    pass


def available() -> bool:
    return bool(secrets.get("FIRECRAWL_API_KEY"))


def _post(path: str, body: dict, timeout: float = 90) -> dict:
    key = secrets.get("FIRECRAWL_API_KEY")
    if not key:
        raise FirecrawlError("no FIRECRAWL_API_KEY (optional; the founder adds it with `bin/gtm secrets set`)")
    r = httpx.post(BASE + path, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    if r.status_code >= 400:
        raise FirecrawlError(f"Firecrawl {path} HTTP {r.status_code}")
    data = r.json()
    if data.get("success") is False:
        raise FirecrawlError(f"Firecrawl {path} refused: {str(data.get('error'))[:200]}")
    return data


def map_site(url: str, limit: int = 100) -> list[str]:
    links = _post("/map", {"url": url, "limit": limit, "includeSubdomains": False}).get("links", [])
    return [x["url"] if isinstance(x, dict) else x for x in links if x]


def scrape(url: str) -> dict:
    data = _post("/scrape", {"url": url, "formats": ["markdown"], "onlyMainContent": True}).get("data", {})
    return {"url": url, "title": (data.get("metadata") or {}).get("title"), "markdown": data.get("markdown") or ""}
