"""Account-first triggers (plan §5 Phase 3): re-profile target accounts, detect changes, jobs, news.

Change detection (Harvey only had it on its roadmap): the latest profile is compared with the
previous one per account, producing TECH_ADDED / TECH_REMOVED / new HIRING_ROLE /
LEADERSHIP_CHANGE, and wigolo `diff` on stored home/pricing text gives SITE_CHANGE /
PRICING_CHANGE. Every finding keeps its evidence URL.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from gtm import observations as obs
from gtm.sources import optional
from gtm.util import domain_of, now, now_iso, sha256

EXEC_WORDS = ("chief", "ceo", "cto", "cmo", "coo", "cfo", "cro", "founder", "president", "vp ",
              "vice president", "head of", "director")
SITE_CHANGE_MIN_CHARS = 400
PROFILES_PER_RUN = {"full": 25, "refresh": 8}


def apply_profile(conn: sqlite3.Connection, prof, *, run_id: str | None, detect_changes: bool = True,
                  collector: str = "profile") -> dict:
    """Record a Profile's findings as observations, update the account, and emit change triggers."""
    domain = prof.domain
    before: dict[str, set] = {}
    if detect_changes:
        for o in obs.latest(conn, domain, only_present=False):
            before.setdefault(o.signal_code, set()).add((o.value_text or "").lower())
    had_profile = bool(before)
    counts = {"findings": 0, "changes": 0}
    for f in prof.findings:
        if obs.record(conn, account_domain=domain, signal_code=f.code, collector=collector,
                      evidence_url=f.evidence_url, confidence=f.confidence, value_num=f.value_num,
                      value_text=f.value_text, run_id=run_id):
            counts["findings"] += 1
    if prof.ok:
        conn.execute("UPDATE accounts SET profile_checked_at = ?, "
                     "name = CASE WHEN name = domain AND ? IS NOT NULL THEN ? ELSE name END, "
                     "linkedin_url = COALESCE(linkedin_url, ?), hq_country = COALESCE(hq_country, ?), "
                     "phone = COALESCE(?, phone), phone_evidence = CASE WHEN ? IS NULL THEN phone_evidence ELSE ? END "
                     "WHERE domain = ?",
                     (now_iso(), prof.company_name, prof.company_name, prof.linkedin_url, prof.hq_country,
                      prof.company_phone, prof.company_phone, prof.company_phone_url, domain))
    else:
        conn.execute("UPDATE accounts SET profile_checked_at = ? WHERE domain = ?", (now_iso(), domain))
    if detect_changes and had_profile and prof.ok:
        counts["changes"] += _diff_triggers(conn, prof, before, run_id)
    if prof.ok:
        counts["changes"] += _page_changes(conn, prof, run_id, enabled=detect_changes and had_profile)
    return counts


def _diff_triggers(conn, prof, before: dict[str, set], run_id) -> int:
    domain, n = prof.domain, 0
    home = prof.final_url or f"https://{domain}/"
    now_tech = {f.value_text.lower(): f for f in prof.findings if f.code == "TECH_STACK" and f.value_text}
    old_tech = before.get("TECH_STACK", set())
    for tool, f in now_tech.items():
        if old_tech and tool not in old_tech:
            n += bool(obs.record(conn, account_domain=domain, signal_code="TECH_ADDED", collector="change",
                                 evidence_url=f.evidence_url, confidence=0.8, value_text=f.value_text, run_id=run_id))
    if now_tech:
        for tool in old_tech - set(now_tech) - {""}:
            n += bool(obs.record(conn, account_domain=domain, signal_code="TECH_REMOVED", collector="change",
                                 evidence_url=home, confidence=0.6, value_text=tool, run_id=run_id))
    old_people = before.get("TEAM_MEMBER_FOUND", set())
    if old_people:
        for f in prof.findings:
            if f.code == "TEAM_MEMBER_FOUND" and f.value_text and f.value_text.lower() not in old_people:
                title = f.value_text.split("|", 1)[-1].lower()
                if any(w in f" {title} " for w in EXEC_WORDS):
                    n += bool(obs.record(conn, account_domain=domain, signal_code="LEADERSHIP_CHANGE",
                                         collector="change", evidence_url=f.evidence_url, confidence=0.6,
                                         value_text=f.value_text, run_id=run_id))
    return n


def _page_changes(conn, prof, run_id, *, enabled: bool) -> int:
    """Keep home/pricing text to diff next time. Untrusted text: stored, never shown in reports."""
    n = 0
    for kind in ("home", "pricing"):
        url = prof.pages.get(kind)
        text = prof.page_text.get(url or "", "")
        if not url or not text:
            continue
        h = sha256(text)
        old = conn.execute("SELECT text_hash, text FROM page_snapshots WHERE url = ?", (url,)).fetchone()
        if enabled and old and old["text_hash"] != h:
            changed = _changed_chars(old["text"], text)
            if changed >= SITE_CHANGE_MIN_CHARS or kind == "pricing":
                code = "PRICING_CHANGE" if kind == "pricing" else "SITE_CHANGE"
                n += bool(obs.record(conn, account_domain=prof.domain, signal_code=code, collector="change",
                                     evidence_url=url, confidence=0.7, value_text=f"{changed} chars changed",
                                     run_id=run_id))
        conn.execute("INSERT INTO page_snapshots (url, account_domain, kind, text_hash, text, fetched_at) "
                     "VALUES (?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET text_hash = excluded.text_hash, "
                     "text = excluded.text, fetched_at = excluded.fetched_at",
                     (url, prof.domain, kind, h, text[:20000], now_iso()))
    return n


def _changed_chars(old: str, new: str) -> int:
    """wigolo diff when available (scheduled runs), difflib otherwise. Both measure the same thing."""
    try:
        from gtm.sources import wigolo
        res = wigolo.diff({"markdown": old}, {"markdown": new}, output="summary")
        if isinstance(res, dict) and isinstance(res.get("summary"), dict):
            return int(res["summary"].get("total_changed_chars", 0))
    except Exception:
        pass
    import difflib
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal")


# ---------------------------------------------------------------- collect step

def collect(ctx) -> None:
    _reprofile(ctx)
    if ctx.mode == "full" or ctx.mode == "refresh":
        optional(ctx, "LinkedIn jobs", _jobs, ctx)
    if ctx.mode == "full":
        _news(ctx)


def _reprofile(ctx) -> None:
    from gtm import profile
    from gtm.accounts import http_client
    days = ctx.cfg.tal.profile_refresh_days
    cutoff = (now() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = ctx.conn.execute(
        "SELECT domain FROM accounts WHERE status NOT IN ('dropped','client') AND "
        "(profile_checked_at IS NULL OR profile_checked_at < ?) ORDER BY tal_tier IS NULL, tal_tier, "
        "prescore DESC LIMIT ?", (cutoff, PROFILES_PER_RUN.get(ctx.mode, 8))).fetchall()
    if not rows:
        return
    from gtm.competitors import signatures
    hosts = signatures(ctx.cfg)  # competitor script hosts: a hit = COMPETITOR_CUSTOMER (free)
    with http_client() as http:
        for r in rows:
            prof = profile.profile(r["domain"], client=http, hiring_keywords=ctx.cfg.signals.hiring_keywords,
                                   competitors=hosts)
            c = apply_profile(ctx.conn, prof, run_id=ctx.run_id)
            ctx.count("profiled")
            ctx.count("trigger_changes", c["changes"])


def _watermark(conn, source: str) -> str | None:
    row = conn.execute("SELECT last_collected_at FROM watermarks WHERE source = ?", (source,)).fetchone()
    return row[0] if row else None


def _set_watermark(conn, source: str) -> None:
    conn.execute("INSERT INTO watermarks (source, last_collected_at) VALUES (?, ?) ON CONFLICT(source) "
                 "DO UPDATE SET last_collected_at = excluded.last_collected_at", (source, now_iso()))


def _jobs(ctx) -> None:
    """LinkedIn jobs for the hiring keywords -> HIRING_ROLE / HIRING_FOR_PROBLEM on known or new accounts."""
    codes = obs.confirmed_codes(ctx.conn)
    keywords = ctx.cfg.signals.hiring_keywords
    if not keywords or not ({"HIRING_ROLE", "HIRING_FOR_PROBLEM"} & codes):
        return
    wm = _watermark(ctx.conn, "apify_jobs")
    date_posted = "past24Hours" if wm and (now().timestamp() - _ts(wm)) < 2 * 86400 else "pastWeek"
    from gtm.sources import apify
    client = apify.client()
    step = "collect"
    per_kw = max(10, min(ctx.cfg.budgets.max_items_per_actor // max(1, len(keywords)), 50))
    for kw in keywords[:5]:
        ref_key = f"jobs:{kw}"
        run_id = ctx.get_ref(step).get(ref_key)
        if not run_id:
            est = apify.estimate("jobs", per_kw)
            cap = ctx.budget.reserve_apify(est, f"jobs '{kw}'")
            run_id = client.start("jobs", apify.jobs_input(kw, date_posted=date_posted, limit=per_kw),
                                  max_items=per_kw, max_charge_usd=cap)
            ctx.set_ref(step, ref_key, run_id)
        info = client.wait(run_id)
        ctx.budget.charge_apify(info.usage_usd, f"jobs '{kw}'", run_id)
        for job in client.items(info, limit=per_kw):
            _record_job(ctx, job, kw)
    _set_watermark(ctx.conn, "apify_jobs")


def _ts(iso: str) -> float:
    from gtm.util import parse_iso
    return parse_iso(iso).timestamp()


def _record_job(ctx, job: dict, keyword: str) -> None:
    domain = domain_of(job.get("companyWebsite") or "")
    link = job.get("link") or job.get("applyUrl") or ""
    title = (job.get("title") or "").strip()[:150]
    if not domain or not link.startswith("http") or not title:
        return
    from gtm import accounts
    accounts.ensure_account(ctx.conn, domain=domain, name=job.get("companyName") or domain, source="signal",
                            evidence=link, linkedin_url=job.get("companyLinkedinUrl"))
    obs.record(ctx.conn, account_domain=domain, signal_code="HIRING_ROLE", collector="apify_jobs",
               evidence_url=link, confidence=0.85, value_text=title, run_id=ctx.run_id)
    obs.record(ctx.conn, account_domain=domain, signal_code="HIRING_FOR_PROBLEM", collector="apify_jobs",
               evidence_url=link, confidence=0.75, value_text=title, value_num=2, run_id=ctx.run_id)
    ctx.count("job_triggers")


def _news(ctx) -> None:
    """wigolo news search for the best TAL accounts -> isolated labelling -> trigger observations."""
    allowed = sorted({"FUNDING", "LEADERSHIP_CHANGE", "LAUNCH", "EXPANSION"} & obs.confirmed_codes(ctx.conn))
    if not allowed or ctx.llm.remaining < 10:
        return
    rows = ctx.conn.execute("SELECT domain, name FROM accounts WHERE source = 'tal' AND status NOT IN "
                            "('dropped','client') ORDER BY tal_tier, prescore DESC LIMIT 8").fetchall()
    if not rows:
        return
    from gtm.schemas import NewsEvents
    from gtm.sources import wigolo
    blocks, companies, urls = {}, [], {}
    for r in rows:
        try:
            res = wigolo.search([f"{r['name']} funding", f"{r['name']} appoints", f"{r['name']} launches"],
                                time_range="month", max_results=4)
        except Exception as e:
            ctx.note(f"news search failed for {r['domain']}: {type(e).__name__}")
            continue
        results = res.get("results", []) if isinstance(res, dict) else []
        urls[r["domain"]] = {x.get("url") for x in results}
        companies.append({"domain": r["domain"], "name": r["name"]})
        blocks[f"news:{r['domain']}"] = "\n\n".join(
            f"URL: {x.get('url')}\nTITLE: {x.get('title')}\nSNIPPET: {x.get('snippet')}" for x in results)
    if not companies:
        return
    out = ctx.llm.json("label_news", {"companies": companies, "allowed_codes": allowed}, NewsEvents,
                       untrusted_blocks=blocks)
    for ev in (out.events if out else []):
        if ev.code in allowed and ev.evidence_url and ev.evidence_url in urls.get(ev.domain, set()):
            if obs.record(ctx.conn, account_domain=ev.domain, signal_code=ev.code, collector="news",
                          evidence_url=ev.evidence_url, confidence=0.7, value_text=ev.summary[:200],
                          run_id=ctx.run_id):
                ctx.count("news_triggers")
