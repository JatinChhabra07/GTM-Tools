"""Accounts: clients (the lookalike engine), the Target Account List, enrichment and pre-score
(plan §5 Phases 2-3).

Free sources first (wigolo search, the site profiler); paid LinkedIn company data only after an
estimate the founder confirmed (`--estimate` then `--confirm`, from Harvey).
"""

from __future__ import annotations

import csv
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from gtm import kb
from gtm import observations as obs
from gtm.config import ProductConfig
from gtm.gates import country_code
from gtm.paths import product_config_dir
from gtm.score import client_prints, firmographics, lookalike, prescore
from gtm.util import domain_of, looks_like_url, now_iso

JUNK_DOMAINS = ("linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com",
                "wikipedia.org", "crunchbase.com", "g2.com", "capterra.com", "clutch.co", "glassdoor.com",
                "indeed.com", "naukri.com", "yelp.com", "trustpilot.com", "reddit.com", "medium.com",
                "github.com", "producthunt.com", "tracxn.com", "zoominfo.com", "apollo.io", "owler.com",
                "ambitionbox.com", "justdial.com", "indiamart.com", "amazon.com", "amazon.in", "flipkart.com",
                "shopify.com", "apps.shopify.com", "google.com", "bing.com", "quora.com", "forbes.com",
                "techcrunch.com", "inc42.com", "yourstory.com", "economictimes.indiatimes.com", "gov.in")
WEBMAIL = ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "proton.me", "icloud.com")


def is_junk(domain: str) -> bool:
    d = domain.lower()
    return (not d or "." not in d or d in WEBMAIL
            or any(d == j or d.endswith("." + j) for j in JUNK_DOMAINS) or d.endswith(".gov") or d.endswith(".edu"))


def ensure_account(conn: sqlite3.Connection, *, domain: str, name: str, source: str, evidence: str,
                   linkedin_url: str | None = None, status: str = "new") -> bool:
    """Insert if new. Returns True when created. Resolution evidence is required (plan §3)."""
    domain = domain_of(domain)
    if not domain or not evidence.startswith("http"):
        return False
    if looks_like_url(name):
        name = domain  # a placeholder: the profiler replaces it with the site's own name
    cur = conn.execute(
        "INSERT OR IGNORE INTO accounts (domain, name, source, resolution_evidence, linkedin_url, status, created_at) "
        "VALUES (?,?,?,?,?,?,?)", (domain, (name or domain)[:120], source, evidence, linkedin_url, status, now_iso()))
    return cur.rowcount == 1


# ---------------------------------------------------------------- clients + near misses

def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def _num(v: str | None) -> float | None:
    try:
        return float(str(v).replace(",", "").replace("$", "").replace("₹", "")) if v else None
    except ValueError:
        return None


def import_clients(conn: sqlite3.Connection, slug: str) -> dict:
    out = {"clients": 0, "near_miss": 0, "skipped": []}
    base = product_config_dir(slug)
    path = base / "clients.csv"
    if path.exists():
        for row in _read_csv(path):
            d = domain_of(row.get("domain", ""))
            if not d:
                out["skipped"].append(row.get("company", "?"))
                continue
            conn.execute("INSERT INTO clients (domain, name, contract_size, retention_months, notes) VALUES (?,?,?,?,?) "
                         "ON CONFLICT(domain) DO UPDATE SET name=excluded.name, contract_size=excluded.contract_size, "
                         "retention_months=excluded.retention_months, notes=excluded.notes",
                         (d, row.get("company") or d, _num(row.get("contract_size")),
                          _num(row.get("retention_months")), row.get("notes")))
            ensure_account(conn, domain=d, name=row.get("company") or d, source="tal", evidence=f"https://{d}/",
                           status="client")
            conn.execute("UPDATE accounts SET status = 'client' WHERE domain = ?", (d,))
            out["clients"] += 1
    path = base / "near_miss.csv"
    if path.exists():
        for row in _read_csv(path):
            d = domain_of(row.get("domain", ""))
            if d and ensure_account(conn, domain=d, name=row.get("company") or d, source="tal",
                                    evidence=f"https://{d}/", status="near_miss"):
                out["near_miss"] += 1
    return out


def http_client():
    """The SSRF-safe client for prospect sites (fixture websites under GTM_FAKE=1)."""
    if os.environ.get("GTM_FAKE") == "1":
        from gtm import fake
        return fake.FakeHTTP()
    from gtm.public_http import PublicClient
    return PublicClient()


def profile_accounts(conn: sqlite3.Connection, cfg: ProductConfig, domains: list[str], run_id: str | None = None) -> int:
    from gtm import profile
    from gtm.competitors import signatures
    from gtm.triggers import apply_profile
    n = 0
    hosts = signatures(cfg)  # competitor script hosts: a hit = COMPETITOR_CUSTOMER (free)
    with http_client() as http:
        for d in domains:
            prof = profile.profile(d, client=http, hiring_keywords=cfg.signals.hiring_keywords,
                                   competitors=hosts)
            apply_profile(conn, prof, run_id=run_id, detect_changes=False)
            n += 1
    return n


# ---------------------------------------------------------------- LinkedIn company enrichment (paid)

def size_band_from_range(rng: dict | None, count: int | None) -> str | None:
    n = (rng or {}).get("start") if rng else None
    n = n if n is not None else count
    if n is None:
        return None
    for lo, hi in ((1, 10), (11, 50), (51, 200), (201, 500), (501, 1000), (1001, 5000)):
        if n <= hi:
            return f"{lo}-{hi}"
    return "5001+"


def linkedin_enrich(conn: sqlite3.Connection, cfg: ProductConfig, domains: list[str], *, budget, run_id=None,
                    ref_get=None, ref_set=None) -> dict:
    """harvestapi/linkedin-company by LinkedIn URL (or name) -> INDUSTRY / SIZE_BAND / HQ_COUNTRY / FUNDING."""
    from gtm.sources import apify
    rows = [conn.execute("SELECT domain, name, linkedin_url FROM accounts WHERE domain = ?", (d,)).fetchone()
            for d in domains]
    rows = [r for r in rows if r]
    if not rows:
        return {"enriched": 0}
    urls = [r["linkedin_url"] for r in rows if r["linkedin_url"]]
    names = [r["name"] for r in rows if not r["linkedin_url"]]
    client = apify.client()
    run = (ref_get() or {}).get("linkedin_company") if ref_get else None
    if not run:
        est = apify.estimate("company", len(rows))
        cap = budget.reserve_apify(est, "LinkedIn company data")
        run = client.start("company", apify.company_input(urls=urls, names=names), max_items=len(rows),
                           max_charge_usd=cap)
        if ref_set:
            ref_set("linkedin_company", run)
    info = client.wait(run)
    budget.charge_apify(info.usage_usd, "LinkedIn company data", run)
    by_url = {(r["linkedin_url"] or "").rstrip("/").lower(): r["domain"] for r in rows}
    by_domain = {r["domain"]: r["domain"] for r in rows}
    n = 0
    for item in client.items(info, limit=len(rows) * 2):
        d = by_url.get((item.get("linkedinUrl") or "").rstrip("/").lower()) or by_domain.get(domain_of(item.get("website") or ""))
        if not d:
            continue
        ev = item.get("linkedinUrl") or f"https://{d}/"
        industry = ", ".join(i.get("name", i) if isinstance(i, dict) else str(i) for i in (item.get("industries") or []))[:120]
        band = size_band_from_range(item.get("employeeCountRange"), item.get("employeeCount"))
        hq = next((loc for loc in item.get("locations") or [] if loc.get("headquarter")), None)
        country = country_code((hq or {}).get("country"))
        conn.execute("UPDATE accounts SET industry = COALESCE(?, industry), size_band = COALESCE(?, size_band), "
                     "hq_country = COALESCE(?, hq_country), linkedin_url = COALESCE(linkedin_url, ?), "
                     "linkedin_company_checked_at = ? WHERE domain = ?",
                     (industry or None, band, country, item.get("linkedinUrl"), now_iso(), d))
        for code, val in (("INDUSTRY", industry), ("SIZE_BAND", band), ("HQ_COUNTRY", country)):
            if val:
                obs.record(conn, account_domain=d, signal_code=code, collector="linkedin_company",
                           evidence_url=ev, confidence=0.9, value_text=val, run_id=run_id)
        last = ((item.get("fundingData") or {}).get("lastFundingRound") or {})
        if last.get("announcedOn"):
            observed = _funding_date(last["announcedOn"])
            if observed:
                obs.record(conn, account_domain=d, signal_code="FUNDING", collector="linkedin_company",
                           evidence_url=ev, confidence=0.8, observed_at=observed,
                           value_text=f"{last.get('fundingType', 'round')}".strip()[:80], run_id=run_id)
        n += 1
    # asked = paid for, matched or not: never buy the same company twice
    conn.executemany("UPDATE accounts SET linkedin_company_checked_at = COALESCE(linkedin_company_checked_at, ?) "
                     "WHERE domain = ?", [(now_iso(), r["domain"]) for r in rows])
    return {"enriched": n, "requested": len(rows), "usd": info.usage_usd}


def _funding_date(v) -> str | None:
    """announcedOn arrives as {"year","month","day"} or a string; the observation is dated by it."""
    try:
        if isinstance(v, dict):
            return f"{int(v['year']):04d}-{int(v.get('month', 1)):02d}-{int(v.get('day', 1)):02d}T00:00:00+00:00"
        return str(v)[:10] + "T00:00:00+00:00"
    except (KeyError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------- pre-score + tiers

def rescore_accounts(conn: sqlite3.Connection, cfg: ProductConfig) -> dict:
    prints = client_prints(conn)
    n_clients = len(prints)
    out = {"scored": 0, "tier1": 0, "tier2": 0, "tier3": 0}
    for a in conn.execute("SELECT * FROM accounts WHERE status NOT IN ('client','dropped')").fetchall():
        f = firmographics(a, obs.latest(conn, a["domain"], kinds=("firmographic",)))
        look = lookalike(f, prints, exclude=a["domain"])
        ps = prescore(cfg, f, look, n_clients)
        tier = 1 if ps >= 80 else 2 if ps >= cfg.scoring.prescore_cutoff else 3
        status = a["status"]
        if status in ("new", "prequalified", "below_cutoff"):
            status = "prequalified" if ps >= cfg.scoring.prescore_cutoff else "below_cutoff"
        conn.execute("UPDATE accounts SET lookalike_score = ?, prescore = ?, tal_tier = ?, status = ? WHERE domain = ?",
                     (round(look, 3), ps, tier, status, a["domain"]))
        out["scored"] += 1
        out[f"tier{tier}"] += 1
    return out


def client_pattern_memories(conn: sqlite3.Connection, slug: str) -> int:
    """Aggregate client fingerprint -> kb `client_pattern` (no personal data, no single client)."""
    prints = client_prints(conn)
    if len(prints) < 3:
        return 0
    def top(values):
        counts: dict[str, int] = {}
        for v in values:
            if v:
                counts[v] = counts.get(v, 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])[:3]
    lines = []
    for label, vals in (("industry", [p.firmo.industry for p in prints]),
                        ("size", [p.firmo.size_band for p in prints]),
                        ("country", [p.firmo.country for p in prints]),
                        ("tool", [t for p in prints for t in p.firmo.tech])):
        t = top(vals)
        if t:
            lines.append(f"Clients by {label}: " + ", ".join(f"{k} ({v}/{len(prints)})" for k, v in t))
    added = 0
    for line in lines:
        added += kb.add(slug, "client_pattern", line)["status"] == "added"
    return added


@dataclass
class Calibration:
    clients_above_p80: float
    near_miss_gap: float | None
    anti_icp_max: float | None
    sample_size: int
    passed: bool
    notes: list[str]


def calibrate(conn: sqlite3.Connection, cfg: ProductConfig) -> Calibration:
    """Leave-one-out (plan §5 Phase 2 exit): ≥80% of clients at or above the 80th percentile of the
    ICP-adjacent sample; near-misses ≥15 points lower on average; anti-ICP all < 40."""
    prints = client_prints(conn)
    notes = []
    def ps_for(row, exclude=None):
        f = firmographics(row, obs.latest(conn, row["domain"], kinds=("firmographic",)))
        return prescore(cfg, f, lookalike(f, prints, exclude=exclude), len(prints) - (1 if exclude else 0))
    clients = conn.execute("SELECT a.* FROM accounts a JOIN clients c ON c.domain = a.domain").fetchall()
    sample = conn.execute("SELECT * FROM accounts WHERE source = 'tal' AND status IN ('new','prequalified','below_cutoff')").fetchall()
    near = conn.execute("SELECT * FROM accounts WHERE status = 'near_miss'").fetchall()
    if len(sample) < 20:
        notes.append(f"sample has {len(sample)} accounts; build the TAL first (need ≥ 50 for the real test)")
    sample_scores = sorted(ps_for(r) for r in sample)
    p80 = sample_scores[int(0.8 * (len(sample_scores) - 1))] if sample_scores else 0
    client_scores = [ps_for(r, exclude=r["domain"]) for r in clients]
    above = sum(s >= p80 for s in client_scores) / len(client_scores) if client_scores else 0.0
    gap = None
    if near and client_scores:
        gap = sum(client_scores) / len(client_scores) - sum(ps_for(r) for r in near) / len(near)
    anti = [r for r in sample if any(a.lower() in (r["industry"] or "").lower() for a in cfg.anti_icp if a)]
    anti_max = max((ps_for(r) for r in anti), default=None)
    passed = (above >= 0.8 and (gap is None or gap >= 15) and (anti_max is None or anti_max < 40)
              and len(sample) >= 20 and len(client_scores) >= 3)
    return Calibration(round(above, 2), None if gap is None else round(gap, 1),
                       None if anti_max is None else round(anti_max, 1), len(sample), passed, notes)


# ---------------------------------------------------------------- TAL candidates (free: wigolo)

def icp_queries(cfg: ProductConfig) -> list[str]:
    names = {"IN": "India", "US": "United States", "GB": "UK", "AE": "UAE", "SG": "Singapore"}
    out = []
    for seg in cfg.icp.segments:
        geos = [names.get(country_code(g) or g, g) for g in seg.geos] or [""]
        for ind in seg.industries[:3] or [seg.name]:
            for geo in geos[:2]:
                out.append(f"{ind} companies {geo}".strip())
                for tech in seg.tech_stack_any[:2]:
                    out.append(f"{ind} brands using {tech} {geo}".strip())
    return list(dict.fromkeys(out))[:12]


_MD_LINK = re.compile(r"\[([^\]\n]{2,80})\]\((https?://[^)\s]+)\)")
_BARE_DOMAIN = re.compile(r"\b((?:[a-z0-9-]+\.)+(?:com|co|io|shop|store|us|in|co\.in|net|org|ai|app|brand))\b", re.I)
MAX_LISTS = 6
MAX_PER_LIST = 40


def _classify(llm_session, cfg: ProductConfig, competitors: list[str], entries: list[dict]):
    from gtm.schemas import Candidates
    block = "\n\n".join(f"URL: {e['url']}\nTITLE: {e.get('title')}\nSNIPPET: {e.get('snippet')}" for e in entries)
    return llm_session.json("classify_candidates", {"icp": cfg.icp.model_dump(), "anti_icp": cfg.anti_icp,
                                                    "competitors": competitors, "product": cfg.product.name},
                            Candidates, untrusted_blocks={"search_results": block})


def list_entries(list_url: str, list_title: str | None) -> list[dict]:
    """Companies named on a list page ("Top 100 Shopify stores", directories): their own links,
    plus bare domains printed in the text. One entry per company domain."""
    from gtm.sources import wigolo
    page = wigolo.fetch(list_url, max_content_chars=60000)
    md = page.get("markdown") or ""
    host = domain_of(list_url)
    out: dict[str, dict] = {}
    pairs = [(name, url) for name, url in _MD_LINK.findall(md)]
    pairs += [(None, u) for u in page.get("links") or [] if isinstance(u, str)]
    pairs += [(None, f"https://{d.lower()}/") for d in _BARE_DOMAIN.findall(md)]
    for name, url in pairs:
        d = domain_of(url)
        if not d or d == host or d.endswith("." + host) or is_junk(d) or d in out:
            continue
        out[d] = {"url": f"https://{d}/", "title": (name or "").strip() or None,
                  "snippet": f"named on the list \"{(list_title or '')[:100]}\""}
        if len(out) >= MAX_PER_LIST:
            break
    return list(out.values())


def candidates_from_search(llm_session, cfg: ProductConfig, queries: list[str], *, competitors: list[str]) -> list[dict]:
    """wigolo search -> isolated classification -> [{domain, name, evidence}] (company sites only).

    Generic searches mostly return LISTS of companies (top-N articles, directories, "sites using
    Shopify"), not company homepages, so lists are mined too: the companies' own links on the page
    are classified the same way, with the list page as evidence."""
    from gtm.sources import wigolo
    found: dict[str, dict] = {}
    lists: dict[str, str | None] = {}

    def accept(out, entries, evidence_for):
        valid = {e["url"] for e in entries}
        for c in (out.companies if out else []):
            if c.url not in valid:
                continue
            if c.is_company_list and c.url not in lists and len(lists) < MAX_LISTS * 2:
                lists[c.url] = next((e.get("title") for e in entries if e["url"] == c.url), None)
            if not c.is_company_site:
                continue
            d = domain_of(c.url)  # the result's own host: the model can't invent a domain here
            if d and not is_junk(d):
                name = c.name if c.name and not looks_like_url(c.name) else d
                found.setdefault(d, {"domain": d, "name": name[:120], "evidence": evidence_for(c.url)})

    for i in range(0, len(queries), 3):
        try:
            res = wigolo.search(queries[i:i + 3], max_results=10)
        except Exception:
            continue
        results = [r for r in (res.get("results", []) if isinstance(res, dict) else []) if r.get("url")]
        if results and llm_session.remaining > 0:
            accept(_classify(llm_session, cfg, competitors, results), results, lambda u: u)
    for list_url, title in list(lists.items())[:MAX_LISTS]:
        if llm_session.remaining <= 0:
            break
        try:
            entries = list_entries(list_url, title)
        except Exception:
            continue
        if entries:
            accept(_classify(llm_session, cfg, competitors, entries), entries, lambda u, src=list_url: src)
    return list(found.values())


def lookalike_queries(conn: sqlite3.Connection) -> list[str]:
    names = [r[0] for r in conn.execute("SELECT name FROM clients ORDER BY contract_size DESC NULLS LAST LIMIT 6")]
    out = []
    for n in names:
        out += [f"companies like {n}", f"{n} competitors"]
    return out
