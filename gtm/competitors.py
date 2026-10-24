"""Competitor engine (plan §15.1 E2): what each competitor sells, and which companies use it.

A, analysis (`gtm competitors analyze`): resolve the competitor's domain (evidence required),
crawl its site and its public review pages, one isolated call per competitor turns them into short
claims, and code keeps only claims citing a page we gave (own-site facts from its own site,
complaints and praise only from review sites). Output: products/<slug>/knowledge/competitors/
<name>.md (every line cites its page), a .json sidecar (the machine-readable part: domain, watched
pages, customer pages, review pages, script hosts) and mem0 `competitor` facts with source URLs.

B, their customers -> accounts (`gtm competitors clients`, plus a weekly free pass in `gtm run`):
- free: logo walls / case studies / testimonials on the competitor's site (1 isolated call to pick
  the real customer names, at most 1 more to resolve names without a link to a domain);
- free: a target site that loads the competitor's script/widget host (`gtm/profile.py`);
- paid, only with --confirm: G2 / Capterra / Trustpilot reviews (Apify). None of these sites
  publishes the reviewer's employer, so a review only becomes an account when its text names the
  reviewer's own company; the reviews' recurring complaints/praise go into the brief either way;
- paid, only with --confirm: LinkedIn/X posts "we use / switching from <competitor>".
Every account found keeps its evidence URL, and observations are only written for codes the
founder confirmed (the database trigger enforces it). The competitor itself never becomes an
account, and gates.py blocks every domain a brief resolved.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from gtm import accounts, discovery, kb
from gtm import observations as obs
from gtm.config import ProductConfig
from gtm.paths import product_config_dir
from gtm.util import days_ago, domain_of, has_pii, normalize_text, now_iso, parse_iso, sha1, sha256

OWN_KINDS = ("positioning", "serves", "feature", "pricing")
REVIEW_KINDS = ("complaint", "praise")
KIND_CAP = {"positioning": 3, "serves": 4, "feature": 8, "pricing": 6, "complaint": 6, "praise": 4}
SECTIONS = (("positioning", "How they describe themselves"), ("serves", "Who they say they serve"),
            ("feature", "Features"), ("pricing", "Pricing (as printed)"),
            ("complaint", "What reviewers complain about"), ("praise", "What reviewers praise"))
MAX_PAGES = 10
PAGE_CHARS = 3500
REVIEW_CHARS = 6000
MIN_PAGE_CHARS = 300
CUSTOMER_PATH = re.compile(r"customer|case-stud|testimonial|success-stor|/clients|wall-of-love|who-uses", re.I)
CUSTOMER_PATHS = ("/customers", "/case-studies", "/testimonials", "/clients")
MAX_CUSTOMER_PAGES = 3
MAX_ENTRIES = 60
MAX_RESOLVE = 10
MAX_COMPETITORS = 4
REVIEWS_PER_SITE = 30
POSTS_PER_RUN = 30
TWEETS_PER_RUN = 50
WEEKLY_DAYS = 6
WATCH_MIN_CHARS = 200  # a feature page edit smaller than this is noise; any pricing edit counts
REVIEW_SITES = {
    "g2": (re.compile(r"https?://(?:www\.)?g2\.com/products/([a-z0-9-]+)", re.I),
           "https://www.g2.com/products/{0}/reviews"),
    "capterra": (re.compile(r"https?://(?:www\.)?capterra\.com/p/(\d+)/([A-Za-z0-9-]+)", re.I),
                 "https://www.capterra.com/p/{0}/{1}/reviews/"),
}
_SRC_HOST = re.compile(r"""src\s*=\s*["']?(?:https?:)?//([a-z0-9.-]+\.[a-z]{2,})""", re.I)
_IMG_LINK = re.compile(r"\[!\[([^\]\n]{0,80})\]\([^)\s]*\)\]\((https?://[^)\s]+)\)")
_IMG = re.compile(r"!\[([^\]\n]{2,80})\]\(([^)\s]*)\)")
_LINK = re.compile(r"(?<!!)\[([^\]\n]{2,80})\]\((https?://[^)\s]+)\)")


@dataclass
class Notes:
    """What `gtm.sources.optional` needs from a run context, for CLI use."""
    notes: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.notes.append(msg)


# ---------------------------------------------------------------- files (the brief + its sidecar)

def name_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "competitor"


def brief_dir(slug: str):
    return product_config_dir(slug) / "knowledge" / "competitors"


def load_all(slug: str) -> list[dict]:
    return kb.competitor_files(slug)


def current(slug: str, cfg: ProductConfig) -> list[dict]:
    """Briefs for the competitors the config lists now (a removed one is no longer analysed or mined)."""
    wanted = {e.strip().lower() for e in cfg.signals.competitors}
    return [b for b in load_all(slug) if (b.get("entry") or "").lower() in wanted][:MAX_COMPETITORS]


def _save(slug: str, data: dict) -> None:
    d = brief_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    base = name_slug(data["name"])
    (d / f"{base}.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (d / f"{base}.md").write_text(render(data), encoding="utf-8")


def known_domains(slug: str) -> set[str]:
    """Every domain a brief resolved (kept even after the founder drops a competitor: still not a prospect)."""
    try:
        return {b["domain"] for b in load_all(slug) if b.get("domain")}
    except Exception:
        return set()


def known_names(slug: str) -> list[str]:
    try:
        return [b["name"] for b in load_all(slug) if b.get("name")]
    except Exception:
        return []


def is_competitor(domain: str, cfg: ProductConfig) -> bool:
    """The competitor itself, any of its subdomains, or our own product: never an account."""
    d = domain_of(domain)
    blocked = known_domains(cfg.product.slug) | {domain_of(c) for c in cfg.signals.competitors if "." in c}
    blocked.add(domain_of(str(cfg.product.url)))
    return any(d == b or d.endswith("." + b) for b in blocked if b)


def signatures(cfg: ProductConfig) -> dict[str, tuple[str, ...]]:
    """competitor name -> script/widget hosts it puts on customer sites, for `profile.profile`.
    From the brief's `script_hosts` (seen in `src=` on its own pages, always its own subdomain), plus
    the profiler's built-in fingerprint when the competitor is a known tool (e.g. Klaviyo)."""
    from gtm.profile import TECH_SIGNATURES
    builtin = {_label(k): v for k, v in TECH_SIGNATURES.items()}
    out: dict[str, tuple[str, ...]] = {}
    briefs = {(b.get("entry") or "").lower(): b for b in load_all(cfg.product.slug)}
    for entry in cfg.signals.competitors:
        b = briefs.get(entry.strip().lower(), {})
        name = b.get("name") or entry.strip()
        hosts = [h["host"] for h in b.get("script_hosts", []) if h.get("host")]
        domain = b.get("domain") or (domain_of(entry) if "." in entry else "")
        for key in (_label(name), _label(domain.split(".")[0]) if domain else ""):
            hosts += list(builtin.get(key, ())) if key else []
        if hosts:
            out[name] = tuple(dict.fromkeys(hosts))
    return out


def render(data: dict) -> str:
    name, domain = data["name"], data.get("domain") or "?"
    lines = [f"# {name} ({domain})", "",
             f"<!-- generated by `bin/gtm competitors analyze` on {data.get('analyzed_at', '')[:10]}. Every line "
             "cites the page that says it; page text was read as data by an isolated model call. -->", ""]
    facts = data.get("facts", []) + data.get("review_facts", [])
    for kind, title in SECTIONS:
        rows = [f for f in facts if f["kind"] == kind]
        if rows:
            lines += [f"## {title}"] + [f"- {f['text']} ([source]({f['source_url']}))" for f in rows] + [""]
    if data.get("changes"):
        lines += ["## Changes noticed"] + [f"- {c['at'][:10]}: {c['kind'].replace('competitor_', '')} page changed "
                                           f"({c['changed_chars']} characters) ([page]({c['url']}))"
                                           for c in data["changes"][-10:]] + [""]
    if data.get("watch"):
        lines += ["## Pages watched for changes"] + [f"- {w['kind'].replace('competitor_', '')}: {w['url']}"
                                                     for w in data["watch"]] + [""]
    if data.get("review_pages"):
        lines += ["## Review pages"] + [f"- {k}: {u}" for k, u in data["review_pages"].items()] + [""]
    if data.get("script_hosts"):
        lines += ["## Their script on customer sites (tech detection)"] + [
            f"- `{h['host']}` (seen on {h['evidence_url']})" for h in data["script_hosts"]] + [""]
    lines += [f"Domain evidence: {data.get('domain_evidence', '-')}", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- A: analysis

def resolve_domain(session, entry: str, cached: dict | None) -> tuple[str, str | None, str | None]:
    """(name, domain, evidence). A domain entry is its own evidence; a name needs a search result
    the isolated resolver picked (≤ 1 call, once: the sidecar remembers it)."""
    entry = entry.strip()
    if cached and cached.get("domain"):
        return cached["name"], cached["domain"], cached.get("domain_evidence")
    if "." in entry and " " not in entry:
        d = domain_of(entry)
        return d, d, f"https://{d}/"
    found = resolve_names(session, [entry])
    if entry in found:
        d, ev = found[entry]
        return entry, d, ev
    return entry, None, None


def _own_pages(domain: str, notes) -> list[dict]:
    from gtm import site_crawl
    root = f"https://{domain}/"
    try:
        pages = site_crawl.via_wigolo(root, MAX_PAGES)
    except Exception as e:
        notes.note(f"crawl of {domain} failed: {type(e).__name__}")
        pages = []
    try:
        pages += site_crawl.key_pages(root, pages)
        pages += _customer_key_pages(root, pages)
    except Exception:
        pass
    seen, out = set(), []
    for p in sorted(pages, key=lambda p: site_crawl._rank(p["url"])):
        if p.get("url") and len((p.get("markdown") or "").strip()) >= MIN_PAGE_CHARS and p["url"] not in seen:
            seen.add(p["url"])
            out.append(p)
    return out[:MAX_PAGES]


def _customer_key_pages(root: str, have: list[dict]) -> list[dict]:
    """site_crawl.key_pages covers /customers and /case-studies; testimonials pages too."""
    from gtm.sources import wigolo
    if any(CUSTOMER_PATH.search(urlparse(p["url"]).path) for p in have):
        return []
    try:
        page = wigolo.fetch(root.rstrip("/") + "/testimonials", max_content_chars=40000)
    except Exception:
        return []
    final = page.get("url") or ""
    ok = domain_of(final) == domain_of(root) and CUSTOMER_PATH.search(urlparse(final).path) and not page.get("error")
    return [{"url": final, "title": page.get("title"), "markdown": page.get("markdown") or ""}] if ok else []


def _label(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def review_pages(name: str, domain: str) -> dict[str, str]:
    """The competitor's G2 / Capterra pages (from search results, slug must match its name) and its
    Trustpilot page (built from its domain: that is Trustpilot's canonical URL)."""
    from gtm.sources import wigolo
    out: dict[str, str] = {}
    labels = {_label(name), _label(domain.split(".")[0])} - {""}
    try:
        label = domain.split(".")[0] if name == domain else name  # "gorgias", not "gorgias.com"
        res = wigolo.search([f"{label} reviews G2", f"{label} reviews Capterra"], max_results=10)
        results = res.get("results", []) if isinstance(res, dict) else []
    except Exception:
        results = []
    for r in results:
        url = r.get("url") or ""
        for site, (pattern, canonical) in REVIEW_SITES.items():
            m = pattern.match(url)
            if site in out or not m:
                continue
            slug = _label(m.group(m.lastindex))
            if len(slug) >= 3 and any(lab in slug or slug in lab for lab in labels):
                out[site] = canonical.format(*m.groups())
    out["trustpilot"] = f"https://www.trustpilot.com/review/{domain}"
    return out


def _fetch_reviews(pages: dict[str, str]) -> list[dict]:
    from gtm.sources import wigolo
    out = []
    for site, url in pages.items():
        try:
            page = wigolo.fetch(url, max_content_chars=REVIEW_CHARS * 2)
        except Exception:
            continue
        md = page.get("markdown") or ""
        status = page.get("http_status") or 200
        if len(md.strip()) >= MIN_PAGE_CHARS and status < 400 and not page.get("error"):
            out.append({"url": url, "site": site, "markdown": md})
    return out


def script_hosts(pages: list[dict], domain: str) -> list[dict]:
    """Hosts the competitor's pages tell customers to load (`src=` in snippets / embed docs). Only
    its OWN subdomains count (never www/the bare domain, never a shared CDN), so a hit on a target's
    site can only mean that target installed the competitor."""
    out: dict[str, str] = {}
    for p in pages:
        for host in _SRC_HOST.findall(p.get("markdown") or ""):
            h = host.lower().rstrip(".")
            if h.endswith("." + domain) and not h.startswith("www.") and h not in out:
                out[h] = p["url"]
    return [{"host": h, "evidence_url": u} for h, u in list(out.items())[:5]]


def _norm_url(u: str) -> str:
    return (u or "").strip().rstrip("/")


_WORD = re.compile(r"[a-z][a-z0-9'-]{3,}")
_NUMBER = re.compile(r"\d[\d,.]*")
# a claim is a fact about the competitor, never a message to us or to the writer
_INSTRUCTION = re.compile(r"\b(ignore|disregard|instruction|prompt|system|assistant|writer|the reader|"
                          r"tell (?:the|them|him|her)|you (?:must|should)|do not|don't)\b", re.I)
GROUNDING = 0.6
# "John at Acme", "Sarah K." style person references: themes are about the product, never a reviewer
_PERSON = re.compile(r"\b[A-Z][a-z]{1,15} (?:[A-Z]\.|at [A-Z]|from [A-Z])")


def grounded(text: str, page: str) -> bool:
    """Most of the claim's words, and every number in it, appear on the page it cites."""
    page_l = page.lower()
    words = set(_WORD.findall(text.lower()))
    page_words = set(_WORD.findall(page_l))
    if not words or len(words & page_words) / len(words) < GROUNDING:
        return False
    return all(n.rstrip(".,") in page_l for n in _NUMBER.findall(text))


def valid_claims(claims, own_pages: dict[str, str], review_pages_text: dict[str, str]) -> list[dict]:
    """Keep a claim only if it cites a page we gave, of the right kind, is supported by that page's
    text (an injected sentence the model made up can't pass), reads as a fact, not an instruction,
    and carries no personal data."""
    own = {_norm_url(u): t for u, t in own_pages.items()}
    rev = {_norm_url(u): t for u, t in review_pages_text.items()}
    out, seen, per_kind = [], set(), {}
    for c in claims:
        src = _norm_url(c.source_url)
        pages = own if c.kind in OWN_KINDS else rev if c.kind in REVIEW_KINDS else {}
        if src not in pages:
            continue
        text = re.sub(r"\s+", " ", c.text or "").strip()
        if not text or len(text) > 300 or has_pii(text) or "<untrusted" in text.lower() or _INSTRUCTION.search(text):
            continue
        if not grounded(text, pages[src]):
            continue
        key = normalize_text(text)
        if key in seen or per_kind.get(c.kind, 0) >= KIND_CAP[c.kind]:
            continue
        seen.add(key)
        per_kind[c.kind] = per_kind.get(c.kind, 0) + 1
        out.append({"kind": c.kind, "text": text, "source_url": c.source_url.strip()})
    return out


def _watch_kind(url: str) -> str | None:
    path = urlparse(url).path.lower()
    if "pricing" in path or "plans" in path:
        return "competitor_pricing"
    if any(k in path for k in ("feature", "product", "platform")):
        return "competitor_features"
    return None


def snapshot(conn: sqlite3.Connection, domain: str, url: str, kind: str, text: str) -> int | None:
    """Store the page text (untrusted: kept only to diff, never shown). Returns the changed size when
    it differs from the last visit enough to matter, else None. Same table/diff as triggers.py."""
    from gtm.triggers import _changed_chars
    h = sha256(text)
    old = conn.execute("SELECT text_hash, text FROM page_snapshots WHERE url = ?", (url,)).fetchone()
    changed = None
    if old and old["text_hash"] != h:
        n = _changed_chars(old["text"], text)
        if n >= WATCH_MIN_CHARS or (kind == "competitor_pricing" and n > 0):
            changed = n
    conn.execute("INSERT INTO page_snapshots (url, account_domain, kind, text_hash, text, fetched_at) "
                 "VALUES (?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET text_hash = excluded.text_hash, "
                 "text = excluded.text, fetched_at = excluded.fetched_at",
                 (url, domain, kind, h, text[:20000], now_iso()))
    return changed


def _change_fact(slug: str, data: dict, change: dict) -> None:
    what = "pricing" if change["kind"] == "competitor_pricing" else "product"
    try:
        kb.add(slug, "competitor", f"{data['name']} changed its {what} page (noticed {change['at'][:10]})",
               source_url=change["url"], extra={"competitor": data["name"]})
    except kb.KBError:
        pass


def analyze(conn: sqlite3.Connection, session, cfg: ProductConfig, entry: str, notes) -> dict:
    """One competitor: ≤ 1 resolve call (first time only) + 1 brief call."""
    from gtm.schemas import CompetitorBrief
    slug = cfg.product.slug
    cached = next((b for b in load_all(slug) if (b.get("entry") or "").lower() == entry.strip().lower()), None)
    name, domain, evidence = resolve_domain(session, entry, cached)
    if not domain:
        notes.note(f"couldn't find {entry}'s own website: ask the founder for its domain and list it as a domain")
        return {"name": entry, "status": "no_domain"}
    if domain == domain_of(str(cfg.product.url)):
        return {"name": entry, "status": "is_our_own_product"}
    own = _own_pages(domain, notes)
    rpages = review_pages(name, domain)
    reviews = _fetch_reviews(rpages)
    if not own and not reviews:
        notes.note(f"{name}: no readable pages on {domain} or review sites")
        return {"name": name, "domain": domain, "status": "nothing_readable"}
    sources = [{"url": p["url"], "type": "own_site"} for p in own] + [
        {"url": r["url"], "type": "review_site", "site": r["site"]} for r in reviews]
    blocks = {f"page:{p['url']}": p["markdown"][:PAGE_CHARS] for p in own}
    blocks.update({f"page:{r['url']}": r["markdown"][:REVIEW_CHARS] for r in reviews})
    out = session.json("competitor_brief", {"competitor": name, "domain": domain, "our_product": cfg.product.name,
                                            "sources": sources}, CompetitorBrief, untrusted_blocks=blocks)
    if out is None:
        notes.note(f"{name}: the brief call gave no usable answer; nothing was changed")
        return {"name": name, "domain": domain, "status": "no_answer"}
    facts = valid_claims(out.claims, {p["url"]: p["markdown"] for p in own}, {r["url"]: r["markdown"] for r in reviews})
    data = dict(cached or {})
    data.update(entry=entry.strip(), name=name, domain=domain, domain_evidence=evidence, analyzed_at=now_iso(),
                pages=[p["url"] for p in own], facts=facts,
                customer_pages=[p["url"] for p in own if CUSTOMER_PATH.search(urlparse(p["url"]).path)][:MAX_CUSTOMER_PAGES],
                review_pages={r["site"]: r["url"] for r in reviews} | {k: v for k, v in rpages.items() if k != "trustpilot"},
                script_hosts=script_hosts(own, domain), changes=list(data.get("changes", [])),
                review_facts=list(data.get("review_facts", [])))
    data["watch"] = [{"url": p["url"], "kind": k} for p in own if (k := _watch_kind(p["url"]))][:4]
    new_changes = []
    for p in own:
        k = _watch_kind(p["url"])
        if k:
            n = snapshot(conn, domain, p["url"], k, p["markdown"])
            if n is not None:
                new_changes.append({"url": p["url"], "kind": k, "changed_chars": n, "at": now_iso()})
    data["changes"] = (data["changes"] + new_changes)[-20:]
    _save(slug, data)
    kbres = kb.replace_competitor_facts(slug, name, data["facts"] + data["review_facts"])
    for c in new_changes:
        _change_fact(slug, data, c)
    return {"name": name, "domain": domain, "status": "ok", "facts": len(facts),
            "by_kind": {k: sum(f["kind"] == k for f in facts) for k, _ in SECTIONS},
            "dropped_claims": len(out.claims) - len(facts), "kb": kbres, "changes": new_changes,
            "brief": str(brief_dir(slug) / f"{name_slug(name)}.md"),
            "customer_pages": data["customer_pages"], "review_pages": data["review_pages"],
            "script_hosts": [h["host"] for h in data["script_hosts"]]}


def watch(conn: sqlite3.Connection, slug: str, data: dict, notes) -> int:
    """Weekly free re-check of the watched pricing/feature pages (wigolo fetch + diff, no model)."""
    from gtm.sources import wigolo
    changes = []
    for w in data.get("watch", []):
        try:
            page = wigolo.fetch(w["url"], max_content_chars=40000)
        except Exception:
            continue
        md = page.get("markdown") or ""
        if len(md.strip()) < MIN_PAGE_CHARS or page.get("error"):
            continue
        n = snapshot(conn, data["domain"], w["url"], w["kind"], md)
        if n is not None:
            changes.append({"url": w["url"], "kind": w["kind"], "changed_chars": n, "at": now_iso()})
    if changes:
        data["changes"] = (data.get("changes", []) + changes)[-20:]
        _save(slug, data)
        for c in changes:
            _change_fact(slug, data, c)
            notes.note(f"{data['name']} changed its {c['kind'].replace('competitor_', '')} page: {c['url']}")
    return len(changes)


# ---------------------------------------------------------------- B: their customers -> accounts

class _Logos(HTMLParser):
    """(img alt, enclosing link) and (link text, href) pairs from raw HTML: logo walls are images,
    and page-to-markdown extraction usually drops them."""

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base, self.pairs, self._a = base, [], None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "a" and a.get("href"):
            self._a = [urljoin(self.base, a["href"].strip()), []]
        elif tag == "img" and a.get("alt", "").strip():
            self.pairs.append((a["alt"].strip(), self._a[0] if self._a else None))
            if self._a:
                self._a[1].append(None)  # this link is a logo: its text isn't a second entry

    def handle_data(self, data):
        if self._a is not None and data.strip():
            self._a[1].append(data.strip())

    def handle_endtag(self, tag):
        if tag == "a" and self._a is not None:
            href, parts = self._a
            if None not in parts and parts:
                self.pairs.append((" ".join(parts), href))
            self._a = None


def logo_entries(md: str, page_url: str, comp_domain: str, html: str = "") -> list[dict]:
    """Names on a customers page: logo alt texts, off-site links, case-study link titles (from the
    page markdown and, when given, its raw HTML)."""
    out: dict[str, dict] = {}
    own_path = urlparse(page_url).path.rstrip("/")

    def add(name: str, link: str | None) -> None:
        name = re.sub(r"\s+", " ", re.sub(r"(?i)\b(logo|icon|image)\b", "", name or "")).strip(" -|:,.")
        if not 2 <= len(name) <= 60 or len(out) >= MAX_ENTRIES:
            return
        if link:
            d = domain_of(link)
            if not d or d == comp_domain or d.endswith("." + comp_domain):
                link = None  # an on-site link: a case-study title, the name is what counts
            elif accounts.is_junk(d):
                return  # a G2 badge, a social profile: not a customer
        key = link and domain_of(link) or normalize_text(name)
        if key not in out and normalize_text(name) not in {normalize_text(e["name"]) for e in out.values()}:
            out[key] = {"name": name, "link": f"https://{domain_of(link)}/" if link else None, "page": page_url}

    def link(text: str, href: str | None) -> None:
        if href and not href.startswith("http"):
            return  # mailto:, tel:, javascript:
        d = domain_of(href) if href else ""
        if href and (d == comp_domain or d.endswith("." + comp_domain)):
            path = urlparse(href).path.rstrip("/")
            # only a case-study / customer story page of theirs, never navigation or this page itself
            if CUSTOMER_PATH.search(path) and path.count("/") >= 2 and path != own_path:
                add(text, None)
        else:
            add(text, href)

    for alt, href in _IMG_LINK.findall(md):
        add(alt, href)
    md = _IMG_LINK.sub(" ", md)  # a linked logo is decided by its link (a G2 badge stays dropped)
    for alt, _src in _IMG.findall(md):
        add(alt, None)
    for text, href in _LINK.findall(md):
        link(text, href)
    if html:
        p = _Logos(page_url)
        try:
            p.feed(html[:1_500_000])
        except (ValueError, AssertionError):
            pass
        for text, href in p.pairs:
            if href is None:
                add(text, None)
            else:
                d = domain_of(href)
                if href.startswith("http") and not (d == comp_domain or d.endswith("." + comp_domain)):
                    add(text, href)  # a logo or link to another company's site
                else:
                    link(text, href)
    return list(out.values())


def _customer_pages(data: dict) -> list[str]:
    """Their customer / case-study pages, else the usual paths; then the home page (logo strips)."""
    pages = list(data.get("customer_pages") or [])
    if not pages and data.get("domain"):
        pages = [f"https://{data['domain']}{p}" for p in CUSTOMER_PATHS]
    return pages[:MAX_CUSTOMER_PAGES] + [f"https://{data['domain']}/"]


def _read_page(wigolo, http, url: str, comp_domain: str) -> tuple[str, str, str] | None:
    """(final url, markdown, raw html) of one of THEIR pages, or None (missing, 404, off-site)."""
    md, final = "", url
    try:
        page = wigolo.fetch(url, max_content_chars=60000)
        status = page.get("http_status") or 200
        if not page.get("error") and status < 400 and domain_of(page.get("url") or url) == comp_domain:
            md, final = page.get("markdown") or "", page.get("url") or url
    except Exception:
        pass
    html = ""
    try:
        raw = http.get(final)
        if 200 <= raw.status < 300 and domain_of(raw.url) == comp_domain and "html" in (raw.content_type or ""):
            html = raw.text
    except Exception:
        pass
    return (final, md, html) if (md or html) else None


def record_customer(conn, cfg: ProductConfig, *, competitor: str, domain: str, name: str, account_evidence: str,
                    evidence: str, confidence: float, collector: str, run_id: str | None,
                    requires: str = "COMPETITOR_CUSTOMER") -> tuple[bool, bool]:
    """(account created, observation recorded). Never the competitor, our product or a junk host, and
    no account unless `requires` (the code this find will be recorded under) is confirmed."""
    d = domain_of(domain)
    if not d or "." not in d or accounts.is_junk(d) or is_competitor(d, cfg) or not obs.is_confirmed(conn, requires):
        return False, False
    created = accounts.ensure_account(conn, domain=d, name=name, source="competitor", evidence=account_evidence)
    if not conn.execute("SELECT 1 FROM accounts WHERE domain = ?", (d,)).fetchone():
        return False, False
    # a person's post is never stored as engine evidence (forget can't reach it): the company's own evidence is
    discovery.record(conn, d, "competitor", evidence_url=account_evidence if collector == "competitor_post" else evidence,
                     detail=f"{competitor}: {collector}")
    rec = obs.record(conn, account_domain=d, signal_code="COMPETITOR_CUSTOMER", collector=collector,
                     evidence_url=evidence, confidence=confidence, value_text=competitor, run_id=run_id)
    return created, rec is not None


def resolve_names(session, names: list[str]) -> dict[str, tuple[str, str]]:
    """Company names -> {name: (domain, evidence_url)}: one wigolo search per name, then ONE isolated
    call (the resolve_employers prompt) for up to MAX_RESOLVE names. The domain must be the host of a
    result URL the model cited: it can't invent one. Single-string queries: in the live check a
    "<name> company" variant pulled unrelated pages into the merged results."""
    from gtm.schemas import EmployerResolutions
    from gtm.sources import wigolo
    names = [n for n in dict.fromkeys(n.strip() for n in names) if n][:MAX_RESOLVE]
    if not names or session.remaining <= 0:
        return {}
    blocks, urls = {}, {}
    for n in names:
        try:
            res = wigolo.search(f"{n} official website", max_results=6)
        except Exception:
            continue
        results = [x for x in (res.get("results", []) if isinstance(res, dict) else [])
                   if x.get("url") and not accounts.is_junk(domain_of(x["url"]))]
        if results:
            urls[n] = {x["url"] for x in results}
            blocks[f"results:{n}"] = "\n\n".join(f"URL: {x['url']}\nTITLE: {x.get('title')}\nSNIPPET: {x.get('snippet')}"
                                                 for x in results)
    if not blocks:
        return {}
    out = session.json("resolve_employers", {"employers": list(urls)}, EmployerResolutions, untrusted_blocks=blocks)
    found = {}
    for r in (out.resolutions if out else []):
        if r.domain and r.evidence_url in urls.get(r.employer, set()) and r.confidence >= 0.7:
            d = domain_of(r.domain)
            ev_host = domain_of(r.evidence_url)
            if d and (ev_host == d or ev_host.endswith("." + d)) and not accounts.is_junk(d):
                found[r.employer] = (d, r.evidence_url)
    return found


def customers_free(conn: sqlite3.Connection, session, cfg: ProductConfig, data: dict, notes,
                   run_id: str | None = None) -> dict:
    """A competitor's customers / case-study / testimonial pages -> accounts. ≤ 2 isolated calls."""
    from gtm.schemas import CustomerMentions
    from gtm.sources import wigolo
    res = {"competitor": data["name"], "pages": 0, "named": 0, "accounts_new": 0, "observations": 0}
    if not obs.is_confirmed(conn, "COMPETITOR_CUSTOMER"):
        res["skipped"] = "COMPETITOR_CUSTOMER not confirmed"
        return res
    entries: list[dict] = []
    blocks: dict[str, str] = {}
    with accounts.http_client() as http:
        pages = [_read_page(wigolo, http, url, data["domain"]) for url in _customer_pages(data)]
    for final, md, html in (p for p in pages if p):
        if len(blocks) >= MAX_CUSTOMER_PAGES or len(entries) >= MAX_ENTRIES:
            break
        found = [e for e in logo_entries(md, final, data["domain"], html)
                 if normalize_text(e["name"]) not in {normalize_text(x["name"]) for x in entries}]
        found = found[:MAX_ENTRIES - len(entries)]
        if not found:
            continue
        lines = []
        for e in found:
            lines.append(f"ENTRY {len(entries)}: NAME: {e['name']} | LINK: {e['link'] or '-'}")
            entries.append(e)
        blocks[f"page:{final}"] = "\n".join(lines)
        res["pages"] += 1
    if not entries or session.remaining <= 0:
        return res
    out = session.json("competitor_customers", {"competitor": data["name"], "entries": len(entries)},
                       CustomerMentions, untrusted_blocks=blocks)
    linked, unlinked = [], []
    for m in (out.mentions if out else []):
        if not (m.is_company and m.relation == "customer" and 0 <= m.entry < len(entries)):
            continue
        e = entries[m.entry]  # the domain comes from the page's own link, never from the model
        name = (m.name or e["name"]).strip()[:120]
        if normalize_text(name) in {normalize_text(data["name"]), normalize_text(data.get("entry", ""))}:
            continue
        (linked if e["link"] else unlinked).append((name, e))
    res["named"] = len(linked) + len(unlinked)
    for name, e in linked:
        c, r = record_customer(conn, cfg, competitor=data["name"], domain=e["link"], name=name,
                               account_evidence=e["page"], evidence=e["page"], confidence=0.8,
                               collector="competitor_site", run_id=run_id)
        res["accounts_new"] += c
        res["observations"] += r
    resolved = resolve_names(session, [n for n, _ in unlinked])
    for name, e in unlinked:
        if name in resolved:
            d, ev = resolved[name]
            c, r = record_customer(conn, cfg, competitor=data["name"], domain=d, name=name, account_evidence=ev,
                                   evidence=e["page"], confidence=0.7, collector="competitor_site", run_id=run_id)
            res["accounts_new"] += c
            res["observations"] += r
    if len(unlinked) > MAX_RESOLVE:
        notes.note(f"{data['name']}: {len(unlinked) - MAX_RESOLVE} more customer names wait for the next pass")
    return res


# ---------------------------------------------------------------- paid: reviews + posts

def paid_plan(conn: sqlite3.Connection, cfg: ProductConfig, briefs: list[dict]) -> list[dict]:
    """What --confirm would buy, with prices (docs/tools.md §2.1, §2.4). Only what a confirmed code
    can use: buying data nothing may record would be wasted money."""
    from gtm.sources import apify
    codes = obs.confirmed_codes(conn)
    n_rev = min(REVIEWS_PER_SITE, cfg.budgets.max_items_per_actor)
    plan = []
    if codes & {"COMPETITOR_REVIEW_NEGATIVE", "COMPETITOR_CUSTOMER"}:
        for b in briefs:
            for site, url in (b.get("review_pages") or {}).items():
                if site in apify.ACTORS:
                    plan.append({"kind": site, "competitor": b["name"], "url": url, "items": n_rev,
                                 "usd": apify.estimate(site, n_rev), "what": f"{site} reviews of {b['name']}"})
    if codes & {"COMPETITOR_CUSTOMER", "COMPETITOR_PAIN", "SWITCHING"} and briefs:
        plan.append({"kind": "posts", "items": POSTS_PER_RUN, "usd": apify.estimate("posts", POSTS_PER_RUN),
                     "what": "LinkedIn posts naming the competitors"})
        plan.append({"kind": "tweets", "items": TWEETS_PER_RUN, "usd": apify.estimate("tweets", TWEETS_PER_RUN),
                     "what": "X posts naming the competitors"})
    return plan


def _run_actor(client, budget, kind: str, run_input: dict, items: int, est: float, what: str) -> list[dict]:
    cap = budget.reserve_apify(est, what)
    run_id = client.start(kind, run_input, max_items=items, max_charge_usd=cap)
    info = client.wait(run_id)
    budget.charge_apify(info.usage_usd, what, run_id)
    return client.items(info, limit=items)


def _review_date(v) -> str | None:
    from datetime import datetime, timezone
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            pass
    try:
        return parse_iso(s if "T" in s else s[:10] + "T00:00:00+00:00").isoformat(timespec="seconds")
    except ValueError:
        return None


def strength(rating: float) -> int:
    return 3 if rating <= 1 else 2 if rating <= 2 else 1


def reviews(conn, session, cfg: ProductConfig, data: dict, plan: list[dict], client, budget, notes,
            run_id: str | None = None) -> dict:
    """Paid: the competitor's newest reviews -> sourced complaint/praise themes for the brief (1 call),
    and an account only when a review names the reviewer's own company (+ ≤ 1 resolve call)."""
    from gtm.schemas import ReviewReading
    from gtm.sources import apify
    res = {"competitor": data["name"], "reviews": 0, "themes": 0, "accounts_new": 0, "observations": 0}
    items: dict[str, dict] = {}
    for p in plan:
        if p.get("competitor") != data["name"] or p["kind"] not in ("g2", "capterra", "trustpilot"):
            continue
        raw = _run_actor(client, budget, p["kind"], apify.reviews_input(p["kind"], p["url"], limit=p["items"]),
                         p["items"], p["usd"], p["what"])
        for it in raw:
            r = apify.review_item(p["kind"], it)
            url = r["url"] if str(r.get("url") or "").startswith("http") else p["url"]
            rid = sha1(p["kind"], str(r.get("id") or url or r.get("title")))[:12]
            if r.get("title") or r.get("text"):
                items[rid] = {**r, "url": url, "site": p["kind"]}
    res["reviews"] = len(items)
    if not items or session.remaining <= 0:
        return res
    blocks = {f"review:{rid}": f"SITE: {r['site']}\nRATING: {r['rating']}/5\nDATE: {r.get('date') or '-'}\n"
                                f"REVIEWER: {r.get('reviewer') or '-'}\nTITLE: {r.get('title') or '-'}\n"
                                f"TEXT: {(r.get('text') or '')[:1200]}" for rid, r in list(items.items())[:40]}
    out = session.json("read_reviews", {"competitor": data["name"], "review_ids": list(blocks)},
                       ReviewReading, untrusted_blocks=blocks)
    if out is None:
        return res
    themes, seen = [], set()
    for t in out.themes:
        r = items.get(t.review_id.removeprefix("review:"))
        text = re.sub(r"\s+", " ", t.text).strip()
        if not r or not text or len(text) > 300 or has_pii(text) or _INSTRUCTION.search(text) \
                or _PERSON.search(text) or normalize_text(text) in seen:
            continue
        seen.add(normalize_text(text))
        if sum(x["kind"] == t.kind for x in themes) < KIND_CAP[t.kind]:
            themes.append({"kind": t.kind, "text": text, "source_url": r["url"]})
    if themes:
        data["review_facts"] = themes
        _save(cfg.product.slug, data)
        kb.replace_competitor_facts(cfg.product.slug, data["name"], data.get("facts", []) + themes)
    res["themes"] = len(themes)
    named = {}
    for rr in out.reviews:
        rid = rr.review_id.removeprefix("review:")
        if rid in items and rr.employer and rr.employer.strip():
            named.setdefault(rr.employer.strip()[:120], []).append(items[rid])
    resolved = resolve_names(session, list(named))
    for employer, revs in named.items():
        if employer not in resolved:
            continue
        d, ev = resolved[employer]
        for r in revs:
            negative = r.get("rating") is not None and r["rating"] <= 3
            # with only COMPETITOR_REVIEW_NEGATIVE confirmed, only an unhappy reviewer's company is worth an account
            requires = "COMPETITOR_CUSTOMER" if obs.is_confirmed(conn, "COMPETITOR_CUSTOMER") else "COMPETITOR_REVIEW_NEGATIVE"
            if requires == "COMPETITOR_REVIEW_NEGATIVE" and not negative:
                continue
            c, rec = record_customer(conn, cfg, competitor=data["name"], domain=d, name=employer, account_evidence=ev,
                                     evidence=r["url"], confidence=0.7, collector=f"review:{r['site']}", run_id=run_id,
                                     requires=requires)
            res["accounts_new"] += c
            res["observations"] += rec
            exists = conn.execute("SELECT 1 FROM accounts WHERE domain = ?", (domain_of(d),)).fetchone()
            if exists and negative:
                s = strength(r["rating"])
                res["observations"] += obs.record(
                    conn, account_domain=domain_of(d), signal_code="COMPETITOR_REVIEW_NEGATIVE",
                    collector=f"review:{r['site']}", evidence_url=r["url"], confidence=0.6 + 0.1 * s,
                    value_num=s, value_text=f"{data['name']} rated {r['rating']:g}/5: {(r.get('title') or '')[:120]}",
                    observed_at=_review_date(r.get("date")), run_id=run_id) is not None
    return res


def post_queries(names: list[str]) -> list[str]:
    out = []
    for n in names[:MAX_COMPETITORS]:
        out += [f'"switching from {n}"', f'"we use {n}"', f'"{n} alternative"']
    return out


def posts(conn, session, cfg: ProductConfig, briefs: list[dict], plan: list[dict], client, budget, notes,
          run_id: str) -> dict:
    """Paid: LinkedIn/X posts naming a competitor -> "we use X" = COMPETITOR_CUSTOMER on the author's
    stated employer now; "leaving/complaining" posts are labelled SWITCHING / COMPETITOR_PAIN and left
    to the next run's resolve step (which finds the person, as for every intent post)."""
    from gtm import signals
    from gtm.schemas import CompetitorPostLabels
    from gtm.sources import apify
    res = {"posts_new": 0, "uses": 0, "intent_posts": 0, "accounts_new": 0, "observations": 0}
    names = [b["name"] for b in briefs]
    queries = post_queries(names)
    new_ids: list[str] = []
    for p in plan:
        if p["kind"] == "posts":
            for it in _run_actor(client, budget, "posts", apify.posts_input(queries, max_posts=p["items"],
                                 posted_limit="month"), p["items"], p["usd"], p["what"]):
                author = it.get("author") or {}
                url = it.get("linkedinUrl") or ""
                if url.startswith("http") and it.get("content") and signals.store_post(
                        conn, source="linkedin", url=url, text=it["content"], posted_at=(it.get("postedAt") or {}).get("date"),
                        author_name=author.get("name"), author_handle=author.get("linkedinUrl"),
                        author_title=author.get("info"), company_page=bool(author.get("universalName")) and not author.get("publicIdentifier"),
                        keywords=queries, run_id=run_id):
                    new_ids.append(sha1("linkedin", url))
        elif p["kind"] == "tweets":
            for t in _run_actor(client, budget, "tweets", apify.tweets_input(terms=queries[:5]), p["items"],
                                p["usd"], p["what"]):
                a = t.get("author") or {}
                if t.get("url") and t.get("text") and not t.get("isRetweet") and signals.store_post(
                        conn, source="x", url=t["url"], text=t["text"], posted_at=t.get("createdAt"),
                        author_name=a.get("name"), author_handle=a.get("url"), author_title=None,
                        company_page=False, keywords=queries, run_id=run_id):
                    new_ids.append(sha1("x", t["url"]))
    res["posts_new"] = len(new_ids)
    codes = obs.confirmed_codes(conn)
    uses: dict[str, list] = {}
    for i in range(0, len(new_ids), signals.LABEL_BATCH):
        if session.remaining <= 1:
            notes.note("isolated-call budget used up: some competitor posts stay unlabelled for the next run")
            break
        batch = [conn.execute("SELECT * FROM posts WHERE id = ?", (pid,)).fetchone() for pid in new_ids[i:i + signals.LABEL_BATCH]]
        blocks = {f"post:{r['id']}": f"AUTHOR: {r['author_name'] or '-'}\nAUTHOR INFO: {r['author_title'] or '-'}\n"
                                     f"TEXT: {r['text_excerpt']}" for r in batch}
        out = session.json("label_competitor_posts", {"competitors": names, "post_ids": [r["id"] for r in batch]},
                           CompetitorPostLabels, untrusted_blocks=blocks)
        by_id = {lab.post_id.removeprefix("post:"): lab for lab in (out.labels if out else [])}
        for r in batch:
            lab = by_id.get(r["id"])
            ok = lab and not lab.drop_reason and lab.competitor in names and lab.relation
            employer = (lab.employer or "").strip()[:120] if ok else ""
            code = {"leaving": "SWITCHING", "complains": "COMPETITOR_PAIN"}.get(lab.relation if ok else None)
            code = code if code in codes else None
            resolv = r["resolvability"]
            if resolv != "company_event":
                resolv = "named_person" if (r["author_name"] and employer) else "anonymous"
            conn.execute("UPDATE posts SET intent_code = ?, intent_strength = ?, employer = ?, resolvability = ?, "
                         "labelled_at = ? WHERE id = ?", (code, 2 if code else None, employer or None, resolv,
                                                          now_iso(), r["id"]))
            res["intent_posts"] += bool(code)
            if ok and lab.relation == "uses" and employer:
                uses.setdefault(employer, []).append((lab.competitor, r["url"]))
    res["uses"] = sum(len(v) for v in uses.values())
    resolved = resolve_names(session, list(uses))
    for employer, hits in uses.items():
        if employer in resolved:
            d, ev = resolved[employer]
            for comp, url in hits:
                c, rec = record_customer(conn, cfg, competitor=comp, domain=d, name=employer, account_evidence=ev,
                                         evidence=url, confidence=0.75, collector="competitor_post", run_id=run_id)
                res["accounts_new"] += c
                res["observations"] += rec
    return res


# ---------------------------------------------------------------- weekly free pass in `gtm run`

WATERMARK = "competitors_weekly"


def collect(ctx) -> None:
    """Full runs, at most weekly: re-check watched pricing/feature pages (free, no model) and, when
    COMPETITOR_CUSTOMER is confirmed, re-mine customer pages (≤ 2 isolated calls per competitor).
    Paid sources never run here: only `gtm competitors clients --confirm` buys them."""
    if ctx.mode != "full":
        return
    row = ctx.conn.execute("SELECT last_collected_at FROM watermarks WHERE source = ?", (WATERMARK,)).fetchone()
    if row and days_ago(row[0]) < WEEKLY_DAYS:
        return
    briefs = current(ctx.slug, ctx.cfg)
    if not briefs:
        return
    mine = obs.is_confirmed(ctx.conn, "COMPETITOR_CUSTOMER")
    for b in briefs:
        ctx.count("competitor_page_changes", watch(ctx.conn, ctx.slug, b, ctx))
        if mine:
            if ctx.llm.remaining < 14:  # qualify + write matter more
                ctx.note("isolated-call budget low: competitor customer pages wait for the next full run")
                return  # no watermark: retried next full run
            r = customers_free(ctx.conn, ctx.llm, ctx.cfg, b, ctx, run_id=ctx.run_id)
            ctx.count("competitor_customers", r["observations"])
            ctx.count("competitor_accounts_new", r["accounts_new"])
    ctx.conn.execute("INSERT INTO watermarks (source, last_collected_at) VALUES (?, ?) ON CONFLICT(source) "
                     "DO UPDATE SET last_collected_at = excluded.last_collected_at", (WATERMARK, now_iso()))
