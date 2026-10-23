"""Resolve (plan §5 Phase 5): explicit evidence only. An employer named by the author is matched
to an official domain from search results, and the evidence URL is required. Anonymous posts never
reach this module; a post without employer evidence stays unresolved.
"""

from __future__ import annotations

import re

from gtm import accounts
from gtm import observations as obs
from gtm.schemas import EmployerResolutions
from gtm.util import domain_of, now, parse_iso, sha1

MAX_EMPLOYERS_PER_RUN = 10
MIN_CONFIDENCE = 0.7


def lead_key(product: str, *, linkedin_url: str | None, name: str, domain: str) -> str:
    if linkedin_url:
        return sha1(product, linkedin_url.rstrip("/").split("?")[0].lower())
    return sha1(product, name.strip().lower(), domain.lower())


def title_from_headline(headline: str | None) -> str | None:
    """'VP Growth at Acme | ex-Foo' -> 'VP Growth'."""
    if not headline:
        return None
    h = re.split(r"\s+(?:at|@)\s+|\s*\|\s*|\s+-\s+", headline, maxsplit=1)[0]
    return h.strip()[:120] or None


def run(ctx) -> None:
    _resolve_posts(ctx)
    _new_accounts(ctx)


def _resolve_posts(ctx) -> None:
    rows = ctx.conn.execute("SELECT * FROM posts WHERE intent_code IS NOT NULL AND account_domain IS NULL "
                            "AND resolvability IN ('named_person','company_event') AND employer IS NOT NULL "
                            "ORDER BY intent_strength DESC, posted_at DESC").fetchall()
    if not rows:
        return
    employers = list(dict.fromkeys(r["employer"] for r in rows))[:MAX_EMPLOYERS_PER_RUN]
    known = {}
    for e in employers:  # an employer we already resolved: no search, no call
        acct = ctx.conn.execute("SELECT domain FROM accounts WHERE lower(name) = lower(?)", (e,)).fetchone()
        if acct:
            known[e] = (acct["domain"], None)
    todo = [e for e in employers if e not in known]
    if todo and ctx.llm.remaining >= 12:
        known.update(_search_and_resolve(ctx, todo))
    for r in rows:
        if r["employer"] not in known:
            continue
        domain, evidence = known[r["employer"]]
        if evidence:
            accounts.ensure_account(ctx.conn, domain=domain, name=r["employer"], source="signal", evidence=evidence)
        lk = None
        if r["resolvability"] == "named_person":
            lk = lead_key(ctx.slug, linkedin_url=r["author_handle"], name=r["author_name"], domain=domain)
            ctx.conn.execute(
                "INSERT OR IGNORE INTO leads (lead_key, account_domain, full_name, first_name, title, linkedin_url, "
                "is_signal_author, created_at) VALUES (?,?,?,?,?,?,1,?)",
                (lk, domain, r["author_name"], (r["author_name"] or "").split(" ")[0],
                 title_from_headline(r["author_title"]),
                 r["author_handle"] if (r["author_handle"] or "").startswith("http") else None,
                 now().isoformat(timespec="seconds")))
            ctx.count("signal_leads")
        obs.record(ctx.conn, account_domain=domain, signal_code=r["intent_code"], collector=f"post:{r['source']}",
                   evidence_url=r["url"], confidence=0.6 + 0.1 * (r["intent_strength"] or 1),
                   value_num=r["intent_strength"], value_text=r["intent_code"].lower(), lead_key=lk,
                   observed_at=_posted_iso(r["posted_at"]), run_id=ctx.run_id)
        ctx.conn.execute("UPDATE posts SET account_domain = ?, lead_key = ? WHERE id = ?", (domain, lk, r["id"]))
        ctx.count("posts_resolved")


def _posted_iso(posted: str | None) -> str | None:
    if not posted:
        return None
    try:
        return parse_iso(posted).isoformat(timespec="seconds")
    except ValueError:
        return None


def _search_and_resolve(ctx, employers: list[str]) -> dict[str, tuple[str, str]]:
    from gtm.sources import wigolo
    blocks, urls = {}, {}
    for e in employers:
        try:
            res = wigolo.search([f"{e} official website", f"{e} company"], max_results=5)
        except Exception:
            continue
        results = [x for x in (res.get("results", []) if isinstance(res, dict) else [])
                   if x.get("url") and not accounts.is_junk(domain_of(x["url"]))]
        urls[e] = {x["url"] for x in results}
        blocks[f"results:{e}"] = "\n\n".join(f"URL: {x['url']}\nTITLE: {x.get('title')}\nSNIPPET: {x.get('snippet')}"
                                             for x in results)
    if not blocks:
        return {}
    out = ctx.llm.json("resolve_employers", {"employers": [e for e in employers if e in urls]},
                       EmployerResolutions, untrusted_blocks=blocks)
    resolved = {}
    for r in (out.resolutions if out else []):
        if r.domain and r.evidence_url and r.evidence_url in urls.get(r.employer, set()) and r.confidence >= MIN_CONFIDENCE:
            d = domain_of(r.domain)
            if d and d == domain_of(r.evidence_url) and not accounts.is_junk(d):
                resolved[r.employer] = (d, r.evidence_url)
    return resolved


def _new_accounts(ctx) -> None:
    """Profile accounts we've never seen (free), LinkedIn company data for the best few (paid), pre-score."""
    fresh = [r["domain"] for r in ctx.conn.execute(
        "SELECT domain FROM accounts WHERE profile_checked_at IS NULL AND status NOT IN ('dropped') LIMIT 30")]
    if fresh:
        ctx.count("profiled", accounts.profile_accounts(ctx.conn, ctx.cfg, fresh, run_id=ctx.run_id))
    need = [r["domain"] for r in ctx.conn.execute(
        "SELECT a.domain FROM accounts a WHERE a.linkedin_company_checked_at IS NULL AND a.status NOT IN "
        "('dropped','client','near_miss') AND EXISTS (SELECT 1 FROM observations o JOIN signal_codes c ON "
        "c.code = o.signal_code WHERE o.account_domain = a.domain AND c.kind IN ('intent','trigger')) LIMIT 25")]
    if need and ctx.budget.apify_left > 0.2:
        accounts.linkedin_enrich(ctx.conn, ctx.cfg, need, budget=ctx.budget, run_id=ctx.run_id,
                                 ref_get=lambda: ctx.get_ref("resolve"),
                                 ref_set=lambda k, v: ctx.set_ref("resolve", k, v))
    res = accounts.rescore_accounts(ctx.conn, ctx.cfg)
    ctx.count("accounts_scored", res["scored"])
