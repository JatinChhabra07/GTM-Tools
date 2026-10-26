"""Run reports and the founder summary (plan §5 Phase 8).

Reports hold only aggregates, signal codes, scores, ids and evidence DOMAINS. Company and person
names are sanitised (letters, digits, basic punctuation, <= 60 chars). Raw scraped text and reply
bodies never appear here: they stay in `gtm review`, in the founder's own terminal, so untrusted
text never enters an interactive Claude session.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import timedelta

from gtm.paths import runs_dir
from gtm.util import domain_of, now, sanitize_name


def manual_actions(ledger_conn: sqlite3.Connection, product: str | None = None) -> list[dict]:
    rows = ledger_conn.execute("SELECT id, product, action, detail, created_at FROM manual_actions WHERE done_at IS NULL "
                               "AND (product IS NULL OR product = ?) ORDER BY id", (product,)).fetchall()
    return [dict(r) for r in rows]


def funnel(conn: sqlite3.Connection, since: str | None = None) -> dict:
    since = since or (now() - timedelta(days=1)).isoformat(timespec="seconds")
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    return {
        "observations": q("SELECT count(*) FROM observations WHERE observed_at >= ?", since),
        "posts": q("SELECT count(*) FROM posts WHERE labelled_at >= ?", since),
        "posts_with_intent": q("SELECT count(*) FROM posts WHERE labelled_at >= ? AND intent_code IS NOT NULL", since),
        "posts_resolvable": q("SELECT count(*) FROM posts WHERE labelled_at >= ? AND resolvability != 'anonymous'", since),
        "accounts_prequalified": q("SELECT count(*) FROM accounts WHERE status = 'prequalified'"),
        "leads_enriched": q("SELECT count(*) FROM leads WHERE enriched_at >= ?", since),
        "emailable_valid": q("SELECT count(*) FROM leads WHERE enriched_at >= ? AND email_class = 'valid'", since),
        "emailable_catchall": q("SELECT count(*) FROM leads WHERE enriched_at >= ? AND email_class = 'catchall'", since),
        "qualified": q("SELECT count(*) FROM scores WHERE created_at >= ? AND verdict = 'keep'", since),
        "pending_review": q("SELECT count(*) FROM sequences WHERE status = 'pending_review'"),
        "approved_waiting": q("SELECT count(*) FROM sequences WHERE status = 'approved'"),
        "pushed": q("SELECT count(*) FROM sequences WHERE pushed_at >= ?", since),
    }


LEAD_COLUMNS = ("company", "domain", "name", "title", "persona", "linkedin_url", "email", "email_class",
                "status", "why_this_company")


def write_leads_csv(conn: sqlite3.Connection, slug: str):
    """Every lead so far -> <data>/<slug>/leads.csv for the founder to open. Names and profiles live
    only in this file, never in a report or on screen. Returns (path, rows)."""
    import csv
    from gtm.paths import product_data_dir
    from gtm.social import _cell
    why = {r["domain"]: r["reason"] for r in conn.execute(
        "SELECT domain, reason FROM shortlist WHERE verdict = 'keep' ORDER BY at")}
    rows = conn.execute(
        "SELECT a.name AS company, l.account_domain AS domain, l.full_name, l.title, l.persona, l.linkedin_url, "
        "l.email, l.email_class, l.status FROM leads l JOIN accounts a ON a.domain = l.account_domain "
        "ORDER BY l.created_at DESC").fetchall()
    path = product_data_dir(slug) / "leads.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)  # quoted: a spreadsheet keeps "Shubham Sharma" in one cell
        w.writerow(LEAD_COLUMNS)
        for r in rows:
            w.writerow([_cell(v) for v in (r["company"], r["domain"], r["full_name"], r["title"], r["persona"],
                                            r["linkedin_url"], r["email"], r["email_class"], r["status"],
                                            why.get(r["domain"]))])
    return path, rows


def write_run_report(ctx, status: str, reason: str | None) -> None:
    usage = ctx.conn.execute("SELECT prompt_id, count(*) n, sum(coalesce(input_tokens,0)) i, sum(coalesce(output_tokens,0)) o, "
                             "sum(outcome != 'ok') bad FROM usage_events WHERE run_id = ? GROUP BY prompt_id",
                             (ctx.run_id,)).fetchall()
    cost = {**ctx.budget.summary(), "llm_calls": ctx.llm.calls, "llm_cap": ctx.llm.cap,
            "llm_by_prompt": {r["prompt_id"]: {"calls": r["n"], "in": r["i"], "out": r["o"], "invalid_or_error": r["bad"]}
                              for r in usage}}
    try:
        from gtm.sources import apify
        if ctx.budget.spent_apify_usd > 0:
            cost["apify_month_to_date_usd"] = apify.client().month_spend_usd()
    except Exception:
        pass
    (ctx.run_dir / "cost.json").write_text(json.dumps(cost, indent=2))
    f = funnel(ctx.conn)
    kept = ctx.conn.execute(
        "SELECT s.lead_key, s.total, s.holdout, l.persona, a.name, a.domain FROM scores s JOIN leads l ON l.lead_key = s.lead_key "
        "JOIN accounts a ON a.domain = l.account_domain WHERE s.run_id = ? AND s.verdict = 'keep' ORDER BY s.total DESC",
        (ctx.run_id,)).fetchall()
    drops = ctx.conn.execute("SELECT drop_reason, count(*) n FROM scores WHERE run_id = ? AND verdict = 'drop' "
                             "GROUP BY drop_reason ORDER BY n DESC LIMIT 10", (ctx.run_id,)).fetchall()
    lines = [f"# Run {ctx.run_id} ({ctx.slug}, {ctx.mode})", "",
             f"- status: **{status}**" + (f" ({reason})" if reason else ""),
             f"- resumed: {ctx.resumed}", f"- isolated calls: {ctx.llm.calls}/{ctx.llm.cap}",
             f"- Apify: ${ctx.budget.spent_apify_usd:.2f} of ${ctx.budget.apify_usd:.2f} · Snov credits: "
             f"{ctx.budget.spent_snov_credits} of {ctx.budget.snov_credits}", "",
             "## Counts", "", *(f"- {k}: {v}" for k, v in sorted(ctx.counts.items())), "",
             "## Funnel (last 24 h)", "", *(f"- {k}: {v}" for k, v in f.items()), ""]
    if kept:
        lines += ["## Qualified leads", "", "| score | holdout | persona | company | domain |", "|---|---|---|---|---|"]
        lines += [f"| {r['total']} | {'yes' if r['holdout'] else ''} | {sanitize_name(r['persona'])} | "
                  f"{sanitize_name(r['name'])} | {domain_of(r['domain'])} |" for r in kept]
        lines.append("")
    if drops:
        lines += ["## Drop reasons", "", *(f"- {sanitize_name(r['drop_reason'], 120)}: {r['n']}" for r in drops), ""]
    if ctx.counts.get("intent_sheet_rows"):
        from gtm.social import sheet_path
        lines += [f"**{ctx.counts['intent_sheet_rows']} new row(s) in the intent sheet** (anonymous intent posts for "
                  f"manual follow-up; nobody on it is emailed): {sheet_path(ctx.slug)}", ""]
    try:
        path, rows = write_leads_csv(ctx.conn, ctx.slug)
        lines += [f"**Leads file** ({len(rows)} lead(s), {sum(1 for r in rows if r['linkedin_url'])} with a LinkedIn "
                  f"profile, {sum(1 for r in rows if r['email'])} with an email): {path}", ""]
    except OSError as e:  # the report must still be written
        ctx.note(f"leads.csv not written: {e}")
    lines += engine_lines(ctx.conn, ctx.ledger, ctx.slug)
    if ctx.notes:
        lines += ["## Notes", "", *(f"- {sanitize_name(n, 200)}" for n in ctx.notes), ""]
    acts = manual_actions(ctx.ledger, ctx.slug)
    if acts:
        lines += ["## Manual actions waiting for you", "", *(f"- [{a['id']}] {a['action']}: {sanitize_name(a['detail'], 200)}"
                                                            for a in acts), ""]
    if f["pending_review"]:
        lines += [f"**{f['pending_review']} email sequence(s) wait for your review:** run `bin/gtm review --product "
                  f"{ctx.slug}` in your own terminal.", ""]
    (ctx.run_dir / "report.md").write_text("\n".join(lines))


# ---------------------------------------------------------------- outcome metrics (plan §4.5)

RETENTION_DAYS = 90


def prune_old_files(slug: str, days: int = RETENTION_DAYS) -> int:
    """Retention (plan §6.1): run folders and cron logs older than `days` are deleted; the intent
    sheet stays, but its rows older than `days` are dropped, and so are stored posts that never
    became a lead. Returns files + rows removed."""
    import shutil
    import time
    from gtm.paths import product_data_dir
    cutoff = time.time() - days * 86400
    removed = 0
    runs = runs_dir(slug)
    if runs.exists():
        for d in runs.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
    cron = product_data_dir(slug) / "cron"
    if cron.exists():
        for f in cron.iterdir():
            if f.suffix == ".log" and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                removed += 1
    from gtm import store
    from gtm.social import prune_posts, prune_sheet
    removed += prune_sheet(slug, days)
    removed += prune_posts(store.open_state(slug), days)
    return removed


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = successes / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round((c - m) / d, 3), round((c + m) / d, 3)


POSITIVE_INTENTS = ("interested", "question", "referral")
NOT_A_REPLY = ("ooo", "bounce")  # auto-replies
MIN_SAMPLE = 50  # delivered: below this the range is too wide to call a winner (/gtm-report)


def outcomes(conn: sqlite3.Connection, ledger_conn: sqlite3.Connection, product: str) -> dict:
    """North star (positive replies per 100 delivered) + guardrails, by segment / persona / holdout."""
    seqs = conn.execute(
        "SELECT s.seq_id, s.holdout, l.persona, json_extract(sc.feature_snapshot_json, '$.segment') segment, "
        f"(SELECT count(*) FROM replies r WHERE r.seq_id = s.seq_id AND r.intent IN {POSITIVE_INTENTS}) pos, "
        "(SELECT count(*) FROM replies r WHERE r.seq_id = s.seq_id AND r.intent = 'opt_out') optout "
        "FROM sequences s JOIN leads l ON l.lead_key = s.lead_key "
        "LEFT JOIN scores sc ON sc.lead_key = s.lead_key AND sc.verdict = 'keep' WHERE s.status = 'pushed'").fetchall()
    led = {r["id"]: r for r in ledger_conn.execute("SELECT id, status FROM account_sequences WHERE product = ?", (product,))}

    def agg(rows):
        n = len(rows)
        delivered = sum(1 for r in rows if led.get(r["seq_id"]) is None or led[r["seq_id"]]["status"] != "bounced")
        pos = sum(1 for r in rows if r["pos"])
        lo, hi = wilson(pos, delivered)
        return {"sent": n, "delivered": delivered, "positive": pos,
                "positive_per_100": round(100 * pos / delivered, 1) if delivered else 0.0,
                "ci95_per_100": [round(100 * lo, 1), round(100 * hi, 1)],
                "bounce_rate": round(1 - delivered / n, 3) if n else 0.0,
                "opt_out_rate": round(sum(1 for r in rows if r["optout"]) / n, 3) if n else 0.0}

    out = {"all": agg(seqs), "holdout": agg([r for r in seqs if r["holdout"]]),
           "graph_kept": agg([r for r in seqs if not r["holdout"]]), "by_persona": {}, "by_segment": {}}
    for key, field in (("by_persona", "persona"), ("by_segment", "segment")):
        groups: dict[str, list] = {}
        for r in seqs:
            groups.setdefault(r[field] or "?", []).append(r)
        out[key] = {sanitize_name(k): agg(v) for k, v in groups.items()}
    reviewed = conn.execute("SELECT count(*) FROM approvals WHERE via = 'tty_pin'").fetchone()[0]
    rejected = conn.execute("SELECT count(*) FROM sequences WHERE status = 'rejected'").fetchone()[0]
    out["review"] = {"approved": reviewed, "rejected": rejected,
                     "rejected_share": round(rejected / (reviewed + rejected), 3) if reviewed + rejected else 0.0}
    return out


# ---------------------------------------------------------------- per-engine funnel (plan §15, D4)

ENGINE_NOTE = ("A company found by several engines counts for each of them (and so do its leads), "
               "so the engine rows can add up to more than the total.")


def engine_funnel(conn: sqlite3.Connection, ledger_conn: sqlite3.Connection, product: str,
                  days: int | None = None) -> dict:
    """Which discovery engine's companies turn into replies: found -> shortlisted -> enriched ->
    qualified -> contacted -> replied -> positive, with 95% ranges. A cohort: companies first found
    in the window (all time by default), followed to today. No winner below MIN_SAMPLE delivered."""
    from gtm import discovery
    since = (now() - timedelta(days=days)).isoformat(timespec="seconds") if days else None
    by_engine = discovery.domains_by_engine(conn, since)
    shortlisted = {r[0] for r in conn.execute("SELECT DISTINCT domain FROM shortlist WHERE verdict = 'keep'")}
    leads = conn.execute("SELECT lead_key, account_domain, enriched_at FROM leads").fetchall()
    qualified = {r[0] for r in conn.execute("SELECT DISTINCT lead_key FROM scores WHERE verdict = 'keep'")}
    bounced = {r[0] for r in ledger_conn.execute(
        "SELECT id FROM account_sequences WHERE product = ? AND status = 'bounced'", (product,))}
    seqs = conn.execute(
        "SELECT s.seq_id, l.account_domain, "
        f"(SELECT count(*) FROM replies r WHERE r.seq_id = s.seq_id AND COALESCE(r.intent, '') NOT IN {NOT_A_REPLY}) replied, "
        f"(SELECT count(*) FROM replies r WHERE r.seq_id = s.seq_id AND r.intent IN {POSITIVE_INTENTS}) pos "
        "FROM sequences s JOIN leads l ON l.lead_key = s.lead_key WHERE s.status = 'pushed'").fetchall()
    has_clients = conn.execute("SELECT 1 FROM clients LIMIT 1").fetchone() is not None
    engines = {}
    for engine, domains in by_engine.items():
        mine = [s for s in seqs if s["account_domain"] in domains]
        delivered = [s for s in mine if s["seq_id"] not in bounced]
        n = len(delivered)
        replied = sum(1 for s in delivered if s["replied"])
        pos = sum(1 for s in delivered if s["pos"])
        r_lo, r_hi = wilson(replied, n)
        p_lo, p_hi = wilson(pos, n)
        row = {"found": len(domains), "shortlisted": len(domains & shortlisted),
               "enriched": sum(1 for x in leads if x["account_domain"] in domains and x["enriched_at"]),
               "qualified": sum(1 for x in leads if x["account_domain"] in domains and x["lead_key"] in qualified),
               "contacted": len(mine), "delivered": n, "replied": replied, "positive": pos,
               "replies_per_100": round(100 * replied / n, 1) if n else 0.0,
               "replies_ci95_per_100": [round(100 * r_lo, 1), round(100 * r_hi, 1)],
               "positive_per_100": round(100 * pos / n, 1) if n else 0.0,
               "positive_ci95_per_100": [round(100 * p_lo, 1), round(100 * p_hi, 1)],
               "too_few_to_tell": n < MIN_SAMPLE}
        if engine == "lookalike" and not domains and not has_clients:
            row["note"] = "skipped: no clients yet (dream customers can seed it: /gtm-clients)"
        engines[engine] = row
    return {"window_days": days, "cohort": "companies first found in the window, followed to today",
            "note": ENGINE_NOTE, "min_delivered_to_compare": MIN_SAMPLE, "engines": engines, **_leader(engines)}


def _leader(engines: dict) -> dict:
    """A winner only when >= 2 engines have enough sends and the best one's whole 95% range of
    positive replies sits above every other's (the /gtm-report small-sample rule)."""
    big = {e: r for e, r in engines.items() if not r["too_few_to_tell"]}
    if len(big) < 2:
        return {"leader": None, "leader_note": "too few sends to tell which engine works best: each engine needs "
                                               f"{MIN_SAMPLE}+ delivered emails before engines are compared"}
    best = max(big, key=lambda e: big[e]["positive_per_100"])
    lo = big[best]["positive_ci95_per_100"][0]
    if all(lo > r["positive_ci95_per_100"][1] for e, r in big.items() if e != best):
        return {"leader": best, "leader_note": f"{best}: its 95% range sits above every other engine with enough sends"}
    return {"leader": None, "leader_note": "the 95% ranges overlap: no clear winner yet"}


def engine_lines(conn: sqlite3.Connection, ledger_conn: sqlite3.Connection, product: str) -> list[str]:
    """The compact run-report section (engines that found something, or have a note)."""
    ef = engine_funnel(conn, ledger_conn, product)
    rows = [(e, r) for e, r in ef["engines"].items() if r["found"] or r.get("note")]
    if not rows:
        return []
    out = ["## Discovery engines (since the start)", "", ENGINE_NOTE, "",
           "| engine | found | shortlisted | enriched | qualified | contacted | replied | positive | positive/100 (95% range) |",
           "|---|---|---|---|---|---|---|---|---|"]
    for e, r in rows:
        rate = ("too few to tell" if r["too_few_to_tell"] else
                f"{r['positive_per_100']} ({r['positive_ci95_per_100'][0]}-{r['positive_ci95_per_100'][1]})")
        out.append(f"| {e} | {r['found']} | {r['shortlisted']} | {r['enriched']} | {r['qualified']} | "
                   f"{r['contacted']} | {r['replied']} | {r['positive']} | {rate} |")
    out += ["", f"- {ef['leader_note']}", *(f"- {e}: {r['note']}" for e, r in rows if r.get("note")), ""]
    return out
