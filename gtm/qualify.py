"""Qualification graph (plan §4.3): the graph-engineering pattern in deterministic Python.

  rubric (written BEFORE scoring) -> hard gates -> deterministic score -> holdout sample
  -> worker (1 call) -> lens reviewers (2 on max5x, 4 on max20x; parallel; prompted to REFUTE)
  -> synth (1 call) -> re-check of needs_rework (<= 1 call) -> gate (code)

Reviewers are ephemeral (each call is fresh and sees only the facts + rubric, never the worker's
reasoning). No model can raise a score: penalties are 0..-40 and the gate is code. A lead that
doesn't converge is dropped, never passed. <= 7 isolated calls per run.
"""

from __future__ import annotations

import json
import random
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from gtm import gates
from gtm import observations as obs
from gtm.schemas import ReviewOut, SynthOut, WorkerOut
from gtm.score import score_lead
from gtm.util import now_iso, sanitize_name

LENSES = {
    "icp_lookalike": "ICP & lookalike: does the company really match a segment and resemble the clients? "
                     "Wrong industry, size or country; a directory, agency or reseller posing as a buyer?",
    "signal_truth": "Signal truth & resolution: is each signal real, recent and about THIS company/person? "
                    "Could the post/job/news belong to a namesake? Is the evidence URL consistent?",
    "person_fit": "Person fit & email ownership: does this person own the problem and have authority? "
                  "Does the email plausibly belong to them (pattern, generic inbox rules)?",
    "risk": "Risk: competitor, existing client, student, job seeker, vendor, reseller, compliance risk, "
            "or a reference that would feel creepy to the recipient.",
}
# max5x keeps 2 reviewers: each covers two lenses (plan §8)
MERGED = {"fit": ("icp_lookalike", "person_fit"), "truth_risk": ("signal_truth", "risk")}


def rubric(ctx, n_clients: int) -> dict:
    s = ctx.cfg.scoring
    return {
        "hard_gates": ["email valid (catch-all only if allowed + score >= 85 + mailbox allows)",
                       "not a client, competitor, suppressed or anti-ICP", "a fresh intent or trigger signal",
                       "title matches a persona", "country allowed by geo policy (unknown = blocked)",
                       "account navigation: one active contact, 90-day re-contact rules"],
        "score_cutoff": s.cutoff, "weights": "icp_fit 30, intent 30+, lookalike auto, authority 10, contactability 10",
        "lookalike_clients": n_clients,
        "reviewers_can": "only lower scores (0..-40) or drop; never raise",
        "drop_if": ["signal about a different company or person", "person can't buy or doesn't own the problem",
                    "competitor / vendor / reseller / agency / student / job seeker",
                    "the only signal is stale or anonymous", "a reference that would read as surveillance"],
    }


def fact_sheet(ctx, lead: sqlite3.Row, account: sqlite3.Row, latest: list, dims: dict) -> str:
    """Plain facts for the graph. Values that came from the web stay inside the untrusted block."""
    lines = [f"lead_key: {lead['lead_key']}",
             f"company: {sanitize_name(account['name'])} ({account['domain']}), industry: {account['industry'] or '?'}, "
             f"size: {account['size_band'] or '?'}, hq: {account['hq_country'] or '?'}, source: {account['source']}",
             f"person: {sanitize_name(lead['full_name'])}, title: {lead['title'] or '?'}, persona: {lead['persona']} "
             f"({lead['persona_match']}), seniority: {lead['seniority']}, country: {lead['country'] or '?'}",
             f"email: class={lead['email_class']}, source={lead['email_source']}, generic_inbox={bool(lead['is_generic_inbox'])}, "
             f"signal_author={bool(lead['is_signal_author'])}",
             f"deterministic dims: {json.dumps(dims)}", "observations (latest, confident):"]
    for o in latest[:25]:
        lines.append(f"- [{o.id}] {o.signal_code} ({o.kind}) {o.value_text or o.value_num or ''} | "
                     f"conf {o.confidence:.2f} | {o.age_days:.0f}d old{' STALE' if not o.fresh else ''} | {o.evidence_url}")
    for p in ctx.conn.execute("SELECT source, text_excerpt FROM posts WHERE lead_key = ? OR account_domain = ? "
                              "ORDER BY posted_at DESC LIMIT 2", (lead["lead_key"], account["domain"])):
        lines.append(f"post ({p['source']}): {p['text_excerpt'][:400]}")
    return "\n".join(lines)


def run(ctx) -> None:
    rows = ctx.conn.execute(
        "SELECT * FROM leads WHERE status = 'enriched' AND email IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM sequences s WHERE s.lead_key = leads.lead_key AND s.status NOT IN ('rejected','cancelled'))"
    ).fetchall()
    if not rows:
        return
    n_clients = ctx.conn.execute("SELECT count(*) FROM clients").fetchone()[0]
    rub = rubric(ctx, n_clients)
    (ctx.run_dir / "rubric.json").write_text(json.dumps(rub, indent=2))

    survivors, sheets, snapshot = [], {}, {}
    for lead in rows:
        account = ctx.conn.execute("SELECT * FROM accounts WHERE domain = ?", (lead["account_domain"],)).fetchone()
        latest = obs.latest(ctx.conn, account["domain"])
        sc = score_lead(ctx.cfg, account=account, lead=lead, obs=latest, n_clients=n_clients)
        g = gates.check_lead(cfg=ctx.cfg, conn=ctx.conn, ledger_conn=ctx.ledger, lead=lead, account=account,
                             observations=latest, score_total=sc.total)
        snapshot[lead["lead_key"]] = {"dims": sc.dims, "weights": sc.weights, "segment": sc.segment,
                                      "observation_ids": sc.observation_ids, "gates": g.checks, "total": sc.total}
        if not g.ok:
            _save(ctx, lead, sc.total, snapshot, "drop", "; ".join(g.failures), holdout=False)
            ctx.count("dropped_gates")
            continue
        if sc.total < ctx.cfg.scoring.cutoff:
            _save(ctx, lead, sc.total, snapshot, "drop", f"score {sc.total} < cutoff {ctx.cfg.scoring.cutoff}", False)
            ctx.count("dropped_score")
            continue
        survivors.append((lead, sc.total))
        sheets[lead["lead_key"]] = fact_sheet(ctx, lead, account, latest, sc.dims)

    if not survivors:
        return
    # holdout is sampled BEFORE the graph: these leads skip the reviewers (measures what the graph adds)
    rng = random.Random(ctx.run_id)
    k = round(len(survivors) * ctx.cfg.scoring.holdout_pct / 100)
    holdout = {lk for lk, _ in rng.sample([(r["lead_key"], s) for r, s in survivors], k)} if k else set()
    graph = [(r, s) for r, s in survivors if r["lead_key"] not in holdout]
    for r, s in survivors:
        if r["lead_key"] in holdout:
            _save(ctx, r, s, snapshot, "keep", None, holdout=True)
            ctx.count("kept_holdout")

    if graph:
        _graph(ctx, graph, sheets, snapshot, rub)


def _graph(ctx, graph, sheets, snapshot, rub) -> None:
    keys = [r["lead_key"] for r, _ in graph]
    blocks = {f"lead:{lk}": sheets[lk] for lk in keys}
    base = {"rubric": rub, "product": ctx.cfg.product.name, "icp": ctx.cfg.icp.model_dump(),
            "personas": [p.model_dump() for p in ctx.cfg.personas], "lead_keys": keys}
    findings: dict[str, list] = {lk: [] for lk in keys}

    worker = ctx.llm.json("qualify_worker", base, WorkerOut, untrusted_blocks=blocks)
    for w in (worker.leads if worker else []):
        if w.lead_key in findings:
            findings[w.lead_key].append({"from": "worker", "verdict": "lower" if w.penalty else "ok",
                                         "penalty": w.penalty, "reason": "; ".join(w.concerns)[:300]})

    lenses = (dict(LENSES) if ctx.cfg.usage.limits()["qualify_reviewers"] >= 4
              else {name: " ".join(LENSES[x] for x in parts) for name, parts in MERGED.items()})

    def review(item):
        name, question = item
        return name, ctx.llm.json("qualify_review", {**base, "lens": name, "lens_questions": question},
                                  ReviewOut, untrusted_blocks=blocks)

    with ThreadPoolExecutor(max_workers=len(lenses)) as pool:
        reviews = list(pool.map(review, lenses.items()))
    for name, out in reviews:
        for f in (out.findings if out else []):
            if f.lead_key in findings:
                findings[f.lead_key].append({"from": name, "verdict": f.verdict, "penalty": f.penalty,
                                             "reason": f.reason[:300]})
    if any(out is None for _, out in reviews):
        ctx.note("a reviewer returned no valid answer: its lens counts as unreviewed (leads need synth + recheck)")

    synth = ctx.llm.json("qualify_synth", {**base, "findings": findings}, SynthOut)
    verdicts = {v.lead_key: v for v in (synth.verdicts if synth else [])}
    rework = [lk for lk in keys if lk in verdicts and verdicts[lk].verdict == "needs_rework"]
    if rework and ctx.llm.remaining >= 1:
        re_out = ctx.llm.json("qualify_recheck", {**base, "lead_keys": rework,
                                                  "open_questions": {lk: findings[lk] for lk in rework}},
                              SynthOut, untrusted_blocks={f"lead:{lk}": sheets[lk] for lk in rework})
        for v in (re_out.verdicts if re_out else []):
            if v.lead_key in rework and v.verdict in ("keep", "drop"):
                verdicts[v.lead_key] = v

    for r, total in graph:
        lk = r["lead_key"]
        v = verdicts.get(lk)
        reviewed = sum(1 for _, out in reviews if out is not None)
        if v is None or v.verdict == "needs_rework" or reviewed == 0:
            _save(ctx, r, total, snapshot, "drop", "unconverged", False, findings[lk])
            ctx.count("dropped_unconverged")
            continue
        final = round(max(0.0, total + min(0, v.penalty)), 1)
        if v.verdict == "drop":
            _save(ctx, r, final, snapshot, "drop", v.reason[:300] or "reviewers", False, findings[lk])
            ctx.count("dropped_review")
        elif final < ctx.cfg.scoring.cutoff:
            _save(ctx, r, final, snapshot, "drop", f"score after review {final} < cutoff", False, findings[lk])
            ctx.count("dropped_review")
        else:
            _save(ctx, r, final, snapshot, "keep", None, False, findings[lk])
            ctx.count("kept")


def _save(ctx, lead, total, snapshot, verdict, reason, holdout, findings=None) -> None:
    snap = snapshot.get(lead["lead_key"], {})
    ctx.conn.execute(
        "INSERT OR REPLACE INTO scores (lead_key, run_id, total, dims_json, gates_json, reviewer_findings_json, "
        "verdict, drop_reason, holdout, feature_snapshot_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (lead["lead_key"], ctx.run_id, total, json.dumps(snap.get("dims", {})), json.dumps(snap.get("gates", {})),
         json.dumps(findings or []), verdict, reason, int(holdout), json.dumps(snap), now_iso()))
    ctx.conn.execute("UPDATE leads SET status = ? WHERE lead_key = ?",
                     ("qualified" if verdict == "keep" else "dropped", lead["lead_key"]))
    with (ctx.run_dir / "scored.jsonl").open("a") as f:
        f.write(json.dumps({"lead_key": lead["lead_key"], "total": total, "verdict": verdict, "reason": reason,
                            "holdout": holdout}) + "\n")
