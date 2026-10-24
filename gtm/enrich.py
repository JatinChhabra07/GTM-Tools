"""Enrich (plan §5 Phase 5): the right person at each account, then a verified email.

Before any paid call: pre-score, global ledger and account navigation. Then personas: the
signal author or the site's team page first (free), Apify LinkedIn employees second. ONE primary
contact per account. Emails: pattern-first candidate -> Snov verify; else Snov finder; else a
generic inbox printed on their own site. Snov task hashes and the Apify run id are saved as
step refs before waiting, so a killed run resumes without paying twice.
"""

from __future__ import annotations

import re
import sqlite3

from gtm import email_finder, gates, ledger
from gtm import observations as obs
from gtm.resolve import lead_key
from gtm.schemas import PersonaMatches
from gtm.sources import optional
from gtm.sources.snov import GENERIC_PREFIXES
from gtm.util import normalize_text, now_iso

EXCLUDE_TITLE = re.compile(r"\b(intern|assistant|student|recruit\w*|freelanc\w*|advisor|adviser|investor|"
                           r"board member|volunteer|trainee|ex-|former)\b", re.I)
ALIASES = {"ceo": "chief executive officer", "cto": "chief technology officer", "cmo": "chief marketing officer",
           "coo": "chief operating officer", "cfo": "chief financial officer", "cro": "chief revenue officer",
           "vp": "vice president", "co-founder": "cofounder", "co founder": "cofounder"}


def excluded(title: str, personas) -> bool:
    """Assistants, interns, recruiters... are never the buyer, unless the founder's own persona titles
    use that word (an HR-tech product does sell to recruiters)."""
    hits = {w.lower()[:6] for w in EXCLUDE_TITLE.findall(title)}
    if not hits:
        return False
    wanted = {w.lower()[:6] for p in personas for t in p.titles for w in EXCLUDE_TITLE.findall(t)}
    return bool(hits - wanted)


def _norm_title(t: str) -> str:
    t = normalize_text(t).replace("&", " and ")
    t = re.sub(r"[^a-z0-9 ]", " ", t.replace("co-founder", "cofounder"))
    words = [ALIASES.get(w, w) for w in t.split()]
    return " ".join(words)


def match_persona(title: str | None, personas) -> tuple[object, str] | None:
    """exact title, then fuzzy (all persona-title words present), never for excluded roles."""
    if not title or excluded(title, personas):
        return None
    nt = _norm_title(title)
    tokens = set(nt.split())
    for p in personas:
        if any(_norm_title(t) == nt for t in p.titles):
            return p, "exact"
    for p in personas:
        for t in p.titles:
            pt = set(_norm_title(t).split()) - {"of", "and", "the"}
            if pt and pt <= tokens:
                return p, "fuzzy"
    return None


def llm_match(ctx, titles: list[str]) -> dict[str, str | None]:
    """Cached isolated fallback for titles that didn't match by text."""
    out, todo = {}, []
    for t in titles:
        row = ctx.conn.execute("SELECT persona FROM persona_cache WHERE title_norm = ?", (_norm_title(t),)).fetchone()
        if row:
            out[t] = row[0]
        elif not excluded(t, ctx.cfg.personas):
            todo.append(t)
    if todo and ctx.llm.remaining >= 12:
        personas = [{"name": p.name, "titles": p.titles, "seniority": p.seniority} for p in ctx.cfg.personas]
        res = ctx.llm.json("persona_match", {"personas": personas, "titles": todo[:30]}, PersonaMatches)
        names = {p.name for p in ctx.cfg.personas}
        for m in (res.matches if res else []):
            persona = m.persona if m.persona in names else None
            out[m.title] = persona
            ctx.conn.execute("INSERT OR REPLACE INTO persona_cache (title_norm, persona, at) VALUES (?,?,?)",
                             (_norm_title(m.title), persona, now_iso()))
    return out


# ---------------------------------------------------------------- account selection

def candidate_pool(ctx, n: int) -> list[sqlite3.Row]:
    """Free checks and free ranking only; the shortlist panel decides who is paid for (plan §15.2)."""
    from gtm import shortlist
    rows = ctx.conn.execute(
        "SELECT a.* FROM accounts a WHERE a.status IN ('prequalified','new') AND a.prescore >= ? "
        "AND NOT EXISTS (SELECT 1 FROM leads l WHERE l.account_domain = a.domain "
        "                AND l.status IN ('enriched','qualified','sequenced','contacted','replied'))",
        (ctx.cfg.scoring.prescore_cutoff,)).fetchall()
    scored = []
    for a in rows:
        latest = obs.latest(ctx.conn, a["domain"], kinds=("intent", "trigger"))
        fresh = [o for o in latest if o.fresh]
        if not fresh or ledger.is_suppressed(ctx.ledger, domain=a["domain"]):
            continue
        if shortlist.cooling_down(ctx, a["domain"]):
            continue
        if ctx.conn.execute("SELECT 1 FROM clients WHERE domain = ?", (a["domain"],)).fetchone():
            continue
        ok, _ = gates.navigation_ok(ctx.ledger, product=ctx.slug, domain=a["domain"], lead_key="",
                                    referral=False, recontact_days=ctx.cfg.scoring.recontact_days)
        if not ok:
            continue
        has_intent = any(o.kind == "intent" for o in fresh)
        best_rank = min(o.rank for o in fresh)
        scored.append((not has_intent, best_rank, -(a["prescore"] or 0), a))
    scored.sort(key=lambda x: x[:3])
    return [x[3] for x in scored[:n]]


# ---------------------------------------------------------------- people

def _people_on_site(ctx, domain: str) -> list[dict]:
    out = []
    for o in obs.latest(ctx.conn, domain, kinds=("contact",)):
        if o.signal_code == "TEAM_MEMBER_FOUND" and o.value_text and "|" in o.value_text:
            name, title = (x.strip() for x in o.value_text.split("|", 1))
            out.append({"name": name, "title": title, "linkedin_url": None, "country": None,
                        "evidence": o.evidence_url, "source": "site"})
    return out


def _people_from_linkedin(ctx, accounts_rows: list[sqlite3.Row]) -> dict[str, list[dict]]:
    from gtm.sources import apify
    with_url = [a for a in accounts_rows if a["linkedin_url"]]
    if not with_url or ctx.budget.apify_left < 0.06:
        return {}
    titles = sorted({t for p in ctx.cfg.personas for t in p.titles})[:20]
    per = 4
    client = apify.client()
    run_id = ctx.get_ref("enrich").get("employees")
    if not run_id:
        cap = ctx.budget.reserve_apify(apify.estimate("employees", per * len(with_url)), "LinkedIn employees")
        run_id = client.start("employees", apify.employees_input([a["linkedin_url"] for a in with_url],
                                                                  job_titles=titles, per_company=per),
                              max_items=per * len(with_url), max_charge_usd=cap)
        ctx.set_ref("enrich", "employees", run_id)
    info = client.wait(run_id)
    ctx.budget.charge_apify(info.usage_usd, "LinkedIn employees", run_id)
    by_url = {a["linkedin_url"].rstrip("/").lower(): a["domain"] for a in with_url}
    out: dict[str, list[dict]] = {}
    for p in client.items(info, limit=per * len(with_url) * 2):
        pos = (p.get("currentPosition") or [{}])[0] if p.get("currentPosition") else {}
        exp = (p.get("experience") or [{}])[0] if p.get("experience") else {}
        comp_url = (exp.get("companyLinkedinUrl") or "").rstrip("/").lower()
        domain = by_url.get(comp_url)
        if not domain:
            company = (pos.get("companyName") or exp.get("companyName") or "").lower()
            domain = next((a["domain"] for a in with_url if a["name"] and a["name"].lower() == company), None)
        if not domain or not p.get("firstName"):
            continue
        out.setdefault(domain, []).append({
            "name": f"{p.get('firstName', '')} {p.get('lastName', '')}".strip(),
            "title": exp.get("position") or p.get("headline"),
            "linkedin_url": p.get("linkedinUrl"),
            "country": ((p.get("location") or {}).get("countryCode")),
            "evidence": p.get("linkedinUrl") or f"https://{domain}/", "source": "linkedin"})
    return out


def _pick(ctx, people: list[dict]) -> tuple[dict, object, str] | None:
    order = {p.name: i for i, p in enumerate(ctx.cfg.personas)}
    best = None
    unmatched = []
    for person in people:
        m = match_persona(person["title"], ctx.cfg.personas)
        if m:
            key = (order[m[0].name], 0 if m[1] == "exact" else 1)
            if best is None or key < best[0]:
                best = (key, person, m[0], m[1])
        elif person["title"]:
            unmatched.append(person)
    if best is None and unmatched:
        llm = llm_match(ctx, [p["title"] for p in unmatched])
        by_name = {p.name: p for p in ctx.cfg.personas}
        for person in unmatched:
            pname = llm.get(person["title"])
            if pname in by_name:
                key = (order[pname], 2)
                if best is None or key < best[0]:
                    best = (key, person, by_name[pname], "llm")
    return (best[1], best[2], best[3]) if best else None


# ---------------------------------------------------------------- run

def run(ctx) -> None:
    from gtm import shortlist
    if not ctx.cfg.personas:
        ctx.note("no personas yet: run /gtm-icp")
        return
    # only accounts the panel picked are paid for; a resumed run may come hours later, so the free
    # checks (suppressed, now a client, navigation) are re-run before paying
    eligible = {a["domain"] for a in candidate_pool(ctx, 10_000)}
    accts = [a for a in shortlist.kept(ctx) if a["domain"] in eligible]
    ctx.count("enrich_accounts", len(accts))
    if not accts:
        return
    need_linkedin = []
    chosen: dict[str, tuple[dict, object, str]] = {}
    for a in accts:
        people = []
        author = ctx.conn.execute("SELECT * FROM leads WHERE account_domain = ? AND is_signal_author = 1 "
                                  "AND status = 'new' LIMIT 1", (a["domain"],)).fetchone()
        if author:
            people.append({"name": author["full_name"], "title": author["title"], "linkedin_url": author["linkedin_url"],
                           "country": author["country"], "evidence": author["linkedin_url"] or a["resolution_evidence"],
                           "source": "signal", "lead_key": author["lead_key"]})
        people += _people_on_site(ctx, a["domain"])
        pick = _pick(ctx, people)
        if pick:
            chosen[a["domain"]] = pick
        else:
            need_linkedin.append(a)
    for domain, people in (optional(ctx, "LinkedIn people search", _people_from_linkedin, ctx, need_linkedin) or {}).items():
        pick = _pick(ctx, people)
        if pick:
            chosen[domain] = pick
    leads = []
    for a in accts:
        if a["domain"] in chosen:
            person, persona, how = chosen[a["domain"]]
            leads.append(_upsert_lead(ctx, a, person, persona, how))
        else:
            g = _generic_lead(ctx, a)
            if g:
                leads.append(g)
            else:
                ctx.count("no_contact_found")
    optional(ctx, "Snov email finding", find_emails, ctx, [lk for lk in leads if lk])


def _upsert_lead(ctx, account, person: dict, persona, how: str) -> str:
    lk = person.get("lead_key") or lead_key(ctx.slug, linkedin_url=person.get("linkedin_url"), name=person["name"],
                                            domain=account["domain"])
    ctx.conn.execute(
        "INSERT INTO leads (lead_key, account_domain, full_name, first_name, title, persona, persona_match, seniority, "
        "linkedin_url, country, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(lead_key) DO UPDATE SET "
        "persona = excluded.persona, persona_match = excluded.persona_match, seniority = excluded.seniority, "
        "title = COALESCE(leads.title, excluded.title), country = COALESCE(leads.country, excluded.country)",
        (lk, account["domain"], person["name"], person["name"].split(" ")[0], person.get("title"), persona.name, how,
         persona.seniority, person.get("linkedin_url"), person.get("country"), now_iso()))
    ev = person.get("evidence") or account["resolution_evidence"]
    if ev and ev.startswith("http"):
        obs.record(ctx.conn, account_domain=account["domain"], signal_code="DECISION_MAKER_TITLE", collector=person["source"],
                   evidence_url=ev, confidence=0.9 if how == "exact" else 0.75,
                   value_text=f"{person['name']} | {person.get('title') or ''}", lead_key=lk, run_id=ctx.run_id)
    return lk


def _generic_lead(ctx, account) -> str | None:
    inbox = next((o for o in obs.latest(ctx.conn, account["domain"], kinds=("contact",))
                  if o.signal_code == "GENERIC_INBOX" and o.value_text), None)
    if not inbox or not ctx.cfg.personas:
        return None
    lk = lead_key(ctx.slug, linkedin_url=None, name="team", domain=account["domain"])
    ctx.conn.execute(
        "INSERT OR IGNORE INTO leads (lead_key, account_domain, full_name, first_name, title, persona, persona_match, "
        "seniority, email, email_source, is_generic_inbox, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
        (lk, account["domain"], f"{account['name']} team", "", None, ctx.cfg.personas[0].name, "fuzzy", "manager",
         inbox.value_text.lower(), "generic_inbox", now_iso()))
    return lk


# ---------------------------------------------------------------- emails

def find_emails(ctx, lead_keys: list[str]) -> None:
    from gtm.sources import snov
    rows = [ctx.conn.execute("SELECT * FROM leads WHERE lead_key = ?", (lk,)).fetchone() for lk in lead_keys]
    rows = [r for r in rows if r and r["email_class"] is None]
    if not rows:
        return
    _learn_patterns(ctx, {r["account_domain"] for r in rows})
    client = snov.client()
    verify: dict[str, str] = {}  # candidate email -> lead_key
    for r in rows:
        if r["is_generic_inbox"]:
            verify[r["email"]] = r["lead_key"]
            continue
        cand, conf = email_finder.candidate(ctx.conn, r["account_domain"], r["full_name"])
        if cand:
            verify[cand] = r["lead_key"]
    results = _verify(ctx, client, list(verify))
    unresolved = []
    for r in rows:
        cand = next((e for e, lk in verify.items() if lk == r["lead_key"]), None)
        res = results.get(cand) if cand else None
        if res and res["email_class"] in ("valid", "catchall"):
            src = "generic_inbox" if r["is_generic_inbox"] else "pattern"
            _set_email(ctx, r, cand, res, src)
            if res["email_class"] == "valid" and not r["is_generic_inbox"]:
                p = email_finder.infer(cand, r["full_name"])
                if p:
                    email_finder.learn(ctx.conn, r["account_domain"], p, "verified")
        elif not r["is_generic_inbox"]:
            unresolved.append(r)
        else:
            ctx.conn.execute("UPDATE leads SET email_class = ?, smtp_status = ? WHERE lead_key = ?",
                             ((res or {}).get("email_class", "unknown"), (res or {}).get("smtp_status"), r["lead_key"]))
    if unresolved:
        _finder(ctx, client, unresolved)
    for lk in lead_keys:
        ctx.conn.execute("UPDATE leads SET status = 'enriched', enriched_at = ? WHERE lead_key = ? AND status = 'new'",
                         (now_iso(), lk))
    ctx.count("leads_enriched", len(lead_keys))


def _learn_patterns(ctx, domains: set[str]) -> None:
    for d in domains:
        if email_finder.get(ctx.conn, d):
            continue
        people = [o.value_text.split("|", 1)[0].strip() for o in obs.latest(ctx.conn, d, kinds=("contact",))
                  if o.signal_code == "TEAM_MEMBER_FOUND" and o.value_text]
        printed = [o.value_text for o in obs.latest(ctx.conn, d, kinds=("contact",))
                   if o.signal_code == "GENERIC_INBOX" and o.value_text]
        email_finder.learn_from_site(ctx.conn, d, printed, people, GENERIC_PREFIXES)


def _verify(ctx, client, emails: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(emails), 10):
        batch = emails[i:i + 10]
        ref = f"verify:{i}"
        task = ctx.get_ref("enrich").get(ref)
        if not task:
            ctx.budget.reserve_snov(len(batch), "email verification")
            task = client.verify_start(batch)
            ctx.set_ref("enrich", ref, task)
        res = client.wait(client.verify_result, task)
        charged = sum(1 for r in res.values() if r["smtp_status"] in ("valid", "not_valid"))
        ctx.budget.charge_snov(charged, "email verification")
        out.update(res)
    return out


def _finder(ctx, client, rows: list[sqlite3.Row]) -> None:
    for i in range(0, len(rows), 10):
        batch = rows[i:i + 10]
        ref = f"find:{i}"
        task = ctx.get_ref("enrich").get(ref)
        if not task:
            ctx.budget.reserve_snov(len(batch), "email finder")
            task = client.find_start([{"first_name": (r["full_name"] or "").split(" ")[0],
                                       "last_name": " ".join((r["full_name"] or "").split(" ")[1:]),
                                       "domain": r["account_domain"]} for r in batch])
            ctx.set_ref("enrich", ref, task)
        res = client.wait(client.find_result, task)
        by_name = {k.strip().lower(): v for k, v in res.items()}  # Snov echoes "first last" as sent
        charged = 0
        for r in batch:
            parts = (r["full_name"] or "").split(" ")
            found = by_name.get(f"{parts[0]} {' '.join(parts[1:])}".strip().lower(), [])
            charged += len(found)
            best = next((f for f in found if f["email_class"] == "valid"), None) or (found[0] if found else None)
            if best and best["email"].endswith("@" + r["account_domain"]):
                _set_email(ctx, r, best["email"], best, "snov_finder")
                if best["email_class"] == "valid":
                    p = email_finder.infer(best["email"], r["full_name"])
                    if p:
                        email_finder.learn(ctx.conn, r["account_domain"], p, "verified")
            else:
                ctx.conn.execute("UPDATE leads SET email_class = 'unknown' WHERE lead_key = ?", (r["lead_key"],))
        ctx.budget.charge_snov(charged, "email finder")


def _set_email(ctx, r, email: str, res: dict, source: str) -> None:
    email = email.lower()
    taken = ctx.conn.execute("SELECT 1 FROM leads WHERE email = ? AND lead_key != ?", (email, r["lead_key"])).fetchone()
    if taken:
        return
    ctx.conn.execute("UPDATE leads SET email = ?, email_source = ?, smtp_status = ?, unknown_status_reason = ?, "
                     "email_class = ? WHERE lead_key = ?",
                     (email, source, res.get("smtp_status"), res.get("unknown_status_reason"), res["email_class"],
                      r["lead_key"]))
    acct = ctx.conn.execute("SELECT resolution_evidence FROM accounts WHERE domain = ?", (r["account_domain"],)).fetchone()
    obs.record(ctx.conn, account_domain=r["account_domain"], signal_code="EMAIL_STATUS", collector="snov",
               evidence_url=acct["resolution_evidence"], confidence=1.0, value_text=res["email_class"],
               lead_key=r["lead_key"], run_id=ctx.run_id)
