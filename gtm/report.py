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

def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = successes / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round((c - m) / d, 3), round((c + m) / d, 3)


def outcomes(conn: sqlite3.Connection, ledger_conn: sqlite3.Connection, product: str) -> dict:
    """North star (positive replies per 100 delivered) + guardrails, by segment / persona / holdout."""
    seqs = conn.execute(
        "SELECT s.seq_id, s.holdout, l.persona, json_extract(sc.feature_snapshot_json, '$.segment') segment, "
        "(SELECT count(*) FROM replies r WHERE r.seq_id = s.seq_id AND r.intent IN ('interested','question','referral')) pos, "
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
