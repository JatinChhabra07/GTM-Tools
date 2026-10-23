"""`gtm crawl` (plan §5 Phase 1): read the founder's product site into products/<slug>/knowledge/pages/.

wigolo first (free, local). If it returns too little (JS-only sites), Firecrawl when its optional
key is set. /gtm-onboard then reads the pages and extracts facts for the KB. The pages are the
founder's own site, but they're still treated as data: nothing in them is followed as instructions.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from gtm.paths import product_config_dir
from gtm.util import domain_of, now_iso, sha1

# pages that teach the most about a product, best first
PRIORITY = ("pricing", "customer", "case-stud", "about", "product", "feature", "solution", "platform",
            "integration", "how-it-works", "security", "faq", "compare", " vs", "alternative", "use-case",
            "industr", "testimonial", "review", "team", "contact")
SKIP = re.compile(r"(/blog/.+/.+|/tag/|/category/|/author/|/page/\d|/careers?/.+|/legal|/privacy|/terms|"
                  r"/cookie|\.(pdf|jpg|png|zip)$|/login|/signin|/signup|/cart|/checkout)", re.I)
MIN_GOOD_CHARS = 300


def pages_dir(slug: str):
    return product_config_dir(slug) / "knowledge" / "pages"


def _rank(url: str) -> tuple[int, int]:
    path = urlparse(url).path.lower()
    hit = next((i for i, k in enumerate(PRIORITY) if k.strip() in path), len(PRIORITY))
    return (0 if path in ("", "/") else 1 + hit, len(path))


def _same_site(url: str, root: str) -> bool:
    return domain_of(url) == domain_of(root)


def _good(pages: list[dict]) -> list[dict]:
    return [p for p in pages if len((p.get("markdown") or "").strip()) >= MIN_GOOD_CHARS]


def via_wigolo(url: str, max_pages: int) -> list[dict]:
    from gtm.sources import wigolo
    # over-fetch, then keep the most informative pages (pricing, customers...) rather than BFS order
    res = wigolo.crawl(url, strategy="bfs", max_pages=min(60, max_pages * 2), max_depth=2)
    return [{"url": p.get("url"), "title": p.get("title"), "markdown": p.get("markdown") or ""}
            for p in (res.get("pages", []) if isinstance(res, dict) else [])
            if p.get("url") and _same_site(p["url"], url) and not SKIP.search(p["url"])]


KEY_PATHS = ("/pricing", "/customers", "/about", "/case-studies", "/features", "/integrations")


def key_pages(url: str, have: list[dict]) -> list[dict]:
    """The pages that matter most, fetched directly when the crawl didn't reach them."""
    from gtm.sources import wigolo
    root = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    seen = {urlparse(p["url"]).path.rstrip("/").lower() for p in have}
    out = []
    for path in KEY_PATHS:
        if any(s.startswith(path) for s in seen):
            continue
        try:
            page = wigolo.fetch(root + path, max_content_chars=40000)
        except Exception:
            continue
        final = page.get("url") or root + path
        if (page.get("markdown") and _same_site(final, url) and urlparse(final).path.rstrip("/") not in ("", "/")
                and not page.get("error")):
            out.append({"url": final, "title": page.get("title"), "markdown": page["markdown"]})
    return out


def via_firecrawl(url: str, max_pages: int) -> list[dict]:
    from gtm.sources import firecrawl
    links = [u for u in firecrawl.map_site(url, limit=200) if _same_site(u, url) and not SKIP.search(u)]
    links = sorted(set(links) | {url}, key=_rank)[:max_pages]
    out = []
    for u in links:
        try:
            out.append(firecrawl.scrape(u))
        except firecrawl.FirecrawlError:
            continue
    return out


def crawl(slug: str, url: str, *, max_pages: int = 25) -> dict:
    from gtm.sources import firecrawl
    report: dict = {"url": url, "engine": "wigolo", "notes": []}
    try:
        pages = via_wigolo(url, max_pages)
        pages += key_pages(url, pages)
    except Exception as e:
        pages = []
        report["notes"].append(f"wigolo failed: {type(e).__name__}: {str(e)[:200]}")
    if len(_good(pages)) < 3:
        if firecrawl.available():
            report["notes"].append(f"wigolo read {len(_good(pages))} useful page(s); trying Firecrawl "
                                   f"(about {max_pages} credits)")
            try:
                fc = via_firecrawl(url, max_pages)
                if len(_good(fc)) > len(_good(pages)):
                    pages, report["engine"] = fc, "firecrawl"
            except firecrawl.FirecrawlError as e:
                report["notes"].append(str(e))
        else:
            report["notes"].append("few readable pages and no Firecrawl key: the founder can add one with "
                                   "`bin/gtm secrets set` (optional), or paste key pages' text into chat")
    pages = sorted(_good(pages), key=lambda p: _rank(p["url"]))
    seen_bodies: set[str] = set()
    unique = []
    for p in pages:  # a soft-404 page comes back identical for every missing path: keep one copy at most
        body = sha1(p["markdown"].strip())
        if body not in seen_bodies:
            seen_bodies.add(body)
            unique.append(p)
    pages = unique[:max_pages]
    out_dir = pages_dir(slug)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.iterdir():
        if old.suffix == ".md":
            old.unlink()
    index = []
    for i, p in enumerate(pages, 1):
        name = f"{i:02d}-" + (re.sub(r"[^a-z0-9]+", "-", urlparse(p["url"]).path.lower()).strip("-") or "home")[:50] + ".md"
        (out_dir / name).write_text(f"<!-- source: {p['url']} | crawled {now_iso()[:10]} via {report['engine']} -->\n"
                                    f"# {p.get('title') or ''}\n\n{p['markdown'][:40000]}\n", encoding="utf-8")
        index.append({"file": name, "url": p["url"], "title": p.get("title"), "chars": len(p["markdown"])})
    (out_dir / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    report.update(pages=len(index), dir=str(out_dir), index=index)
    return report
