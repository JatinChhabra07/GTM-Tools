"""Shortlist before paying (plan §15.2): an account-level graph picks the best of the pool.

  pool (<= discovery.shortlist_pool, free checks + free ranking) -> worker (1 call)
  -> lens reviewers (2 on max5x, 3 on max20x; parallel; prompted to REFUTE) -> synth (1 call)
  -> code keeps at most the enrich quota, by prescore + penalty

Only kept accounts reach Apify/Snov. No usable panel answer = nothing is paid for this run.
Dropped accounts sit out `shortlist_cooldown_days` unless a newer observation arrives.
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from gtm import observations as obs
from gtm.schemas import ShortlistReviewOut, ShortlistSynthOut, ShortlistWorkerOut
from gtm.util import now, now_iso, sanitize_name

LENSES = {
    "icp_lookalike": "ICP & lookalike: does the company really match a segment and resemble the clients? "
                     "Wrong industry, size or country; a directory, agency or reseller posing as a buyer?",
    "signal_truth": "Signal truth & resolution: is each signal real, recent and about THIS company? "
                    "Could the post/job/news belong to a namesake? Is the evidence URL consistent?",
    "risk": "Risk: competitor, existing client, vendor, reseller, compliance risk, or a company we "
            "could only reach with a reference that would feel creepy.",
}
# calls kept for qualify + write after the panel: never pay for leads this run can't check and write
DOWNSTREAM_RESERVE = 12
# max5x keeps 2 reviewers
MERGED = {"fit": ("icp_lookalike",), "truth_risk": ("signal_truth", "risk")}


def rubric(ctx, n_clients: int, quota: int) -> dict:
    return {
        "goal": f"pick at most {quota} companies worth paying to find a contact at; quality over volume",
        "already_checked_in_code": ["prescore >= cutoff", "a fresh intent or trigger signal", "not a client",
                                    "not suppressed", "account navigation (one active contact, re-contact rules)"],
        "lookalike_clients": n_clients,
        "reviewers_can": "only lower scores (0..-40) or drop; never raise",
        "drop_if": ["signal about a different company", "competitor / vendor / reseller / agency / directory",
                    "clearly outside the ICP (industry, size, country)", "the only signal is stale or second-hand"],
    }


def fact_sheet(ctx, account: sqlite3.Row) -> str:
    """Plain facts for the panel. Values that came from the web stay inside the untrusted block."""
    lines = [f"domain: {account['domain']}",
             f"company: {sanitize_name(account['name'])}, industry: {account['industry'] or '?'}, "
             f"size: {account['size_band'] or '?'}, hq: {account['hq_country'] or '?'}",
             f"source: {account['source']}, tier: {account['tal_tier'] or '?'}, prescore: {account['prescore']}, "
             f"lookalike: {account['lookalike_score'] if account['lookalike_score'] is not None else '?'}",
             "observations (latest, confident):"]
    for o in obs.latest(ctx.conn, account["domain"])[:25]:
        lines.append(f"- {o.signal_code} ({o.kind}) {o.value_text or o.value_num or ''} | conf {o.confidence:.2f} | "
                     f"{o.age_days:.0f}d old | {o.evidence_url}")
    for p in ctx.conn.execute("SELECT source, text_excerpt FROM posts WHERE account_domain = ? "
                              "ORDER BY posted_at DESC LIMIT 2", (account["domain"],)):
        lines.append(f"post ({p['source']}): {p['text_excerpt'][:400]}")
    return "\n".join(lines)


def lenses(ctx) -> dict[str, str]:
    if ctx.cfg.usage.limits()["qualify_reviewers"] >= 4:
        return dict(LENSES)
    return {name: " ".join(LENSES[x] for x in parts) for name, parts in MERGED.items()}


def run(ctx) -> None:
    from gtm import enrich
    from gtm.run import enrich_quota
    if not ctx.cfg.personas:
        return  # enrich notes it
    quota = enrich_quota(ctx)
    if quota <= 0:
        return
    pool = enrich.candidate_pool(ctx, ctx.cfg.discovery.shortlist_pool)
    ctx.count("shortlist_pool", len(pool))
    if not pool:
        return
    review_lenses = lenses(ctx)
    need = len(review_lenses) + 2 + DOWNSTREAM_RESERVE
    if ctx.llm.remaining < need:
        ctx.note(f"shortlist skipped: {ctx.llm.remaining} isolated calls left, the panel plus checking and "
                 f"writing need {need}; nothing paid for this run")
        return

    n_clients = ctx.conn.execute("SELECT count(*) FROM clients").fetchone()[0]
    rub = rubric(ctx, n_clients, quota)
    domains = [a["domain"] for a in pool]
    blocks = {f"account:{a['domain']}": fact_sheet(ctx, a) for a in pool}
    base = {"rubric": rub, "product": ctx.cfg.product.name, "icp": ctx.cfg.icp.model_dump(),
            "anti_icp": ctx.cfg.anti_icp, "personas": [p.model_dump() for p in ctx.cfg.personas],
            "domains": domains}
    findings: dict[str, list] = {d: [] for d in domains}

    worker = ctx.llm.json("shortlist_worker", base, ShortlistWorkerOut, untrusted_blocks=blocks)
    for w in (worker.accounts if worker else []):
        if w.domain in findings:
            findings[w.domain].append({"from": "worker", "verdict": "lower" if w.penalty else "ok",
                                       "penalty": w.penalty, "reason": "; ".join(w.concerns)[:300]})

    def review(item):
        name, question = item
        return name, ctx.llm.json("shortlist_review", {**base, "lens": name, "lens_questions": question},
                                  ShortlistReviewOut, untrusted_blocks=blocks)

    with ThreadPoolExecutor(max_workers=len(review_lenses)) as pool_exec:
        reviews = list(pool_exec.map(review, review_lenses.items()))
    reviewed = sum(1 for _, out in reviews if out is not None)
    for name, out in reviews:
        for f in (out.findings if out else []):
            if f.domain in findings:
                findings[f.domain].append({"from": name, "verdict": f.verdict, "penalty": f.penalty,
                                           "reason": f.reason[:300]})

    # every lens must answer: a lens that didn't is an unreviewed risk, and we don't pay on half a review
    # reviewers wrote their reasons after reading scraped pages: the synth gets them as data, not as instructions
    synth = (ctx.llm.json("shortlist_synth", base, ShortlistSynthOut,
                          untrusted_blocks={"reviewer_findings": json.dumps(findings, indent=1)})
             if reviewed == len(review_lenses) else None)
    if synth is None:
        ctx.note("shortlist panel gave no usable answer: nothing paid for this run")
        for a in pool:
            _save(ctx, a, "not_picked", None, "panel gave no answer", findings[a["domain"]])
        return
    verdicts = {v.domain: v for v in synth.verdicts}

    passed = []
    for i, a in enumerate(pool):
        v = verdicts.get(a["domain"])
        if v is None:
            _save(ctx, a, "not_picked", None, "no verdict from the panel", findings[a["domain"]])
            continue
        final = round(max(0.0, (a["prescore"] or 0) + min(0, v.penalty)), 1)
        if v.verdict == "drop":
            _save(ctx, a, "drop", final, v.reason[:300] or "reviewers", findings[a["domain"]])
            ctx.count("shortlist_dropped")
        else:
            passed.append((-final, i, a, final))
    passed.sort(key=lambda x: x[:2])
    for rank, (_, _, a, final) in enumerate(passed):
        if rank < quota:
            _save(ctx, a, "keep", final, None, findings[a["domain"]])
            ctx.count("shortlist_kept")
        else:
            _save(ctx, a, "not_picked", final, f"beyond today's quota of {quota}", findings[a["domain"]])
            ctx.count("shortlist_not_picked")
    (ctx.run_dir / "shortlist_rubric.json").write_text(json.dumps(rub, indent=2))


def _save(ctx, account, verdict: str, final, reason, findings) -> None:
    ctx.conn.execute(
        "INSERT OR REPLACE INTO shortlist (domain, run_id, verdict, prescore, final, reason, findings_json, at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (account["domain"], ctx.run_id, verdict, account["prescore"], final, reason,
         json.dumps(findings or []), now_iso()))
    with (ctx.run_dir / "shortlist.jsonl").open("a") as f:
        f.write(json.dumps({"domain": account["domain"], "verdict": verdict, "final": final,
                            "reason": reason}) + "\n")


def kept(ctx) -> list[sqlite3.Row]:
    return ctx.conn.execute(
        "SELECT a.* FROM shortlist s JOIN accounts a ON a.domain = s.domain "
        "WHERE s.run_id = ? AND s.verdict = 'keep' ORDER BY s.final DESC, a.domain", (ctx.run_id,)).fetchall()


def cooling_down(ctx, domain: str) -> bool:
    """Dropped by the panel within the cooldown, and no new intent/trigger signal since."""
    since = (now() - timedelta(days=ctx.cfg.discovery.shortlist_cooldown_days)).isoformat(timespec="seconds")
    row = ctx.conn.execute("SELECT max(at) AS at FROM shortlist WHERE domain = ? AND verdict = 'drop' AND at >= ?",
                           (domain, since)).fetchone()
    if not row or not row["at"]:
        return False
    # only a new buying signal reopens it: re-profiling (firmographics) or a weekly re-mined
    # competitor-customer fact is not news
    newer = ctx.conn.execute(
        "SELECT 1 FROM observations o JOIN signal_codes c ON c.code = o.signal_code WHERE o.account_domain = ? "
        "AND o.observed_at > ? AND c.kind IN ('intent','trigger') LIMIT 1", (domain, row["at"])).fetchone()
    return newer is None
