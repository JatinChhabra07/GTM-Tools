"""`gtm feedback` (daily, plan §5 Phase 9): reply classification, reply drafts, referrals, learning.

Order per reply (Harvey's handler, fixed): opt-outs and escalation words were already handled in
the deterministic sync. Here the isolated classifier labels the rest; a classifier failure is
`needs_human`. Drafts are hash-bound and reviewed in `gtm review` like sequences. Proposals and
`learning` memories need >= 100 sends and >= 5 positive replies in the segment (plan §4.5).
"""

from __future__ import annotations

import json

from gtm import config, kb, ledger, llm, store
from gtm.paths import product_config_dir
from gtm.report import outcomes
from gtm.resolve import lead_key
from gtm.schemas import ReplyClass, ReplyDraftOut
from gtm.util import now_iso, sha1, sha256

MIN_SENDS = 100
MIN_POSITIVE = 5
DRAFT_INTENTS = ("interested", "question", "objection", "not_now", "referral")


def draft_hash(draft_id: str, reply_id: str, recipient: str, mailbox: str, body: str) -> str:
    return sha256(draft_id, reply_id, recipient.lower(), mailbox, body)


def run(slug: str) -> dict:
    cfg = config.load(slug)
    conn = store.open_state(slug)
    led = ledger.open_ledger()
    session = llm.Session(conn, run_id=f"feedback-{now_iso()[:10]}", cap=min(40, cfg.usage.limits()["llm_calls_per_run"]),
                          model=cfg.usage.model, pause_minutes=cfg.usage.usage_limit_pause_minutes)
    out = {"classified": 0, "drafts": 0, "referrals": 0, "needs_human": 0}
    facts = [m["text"] for m in kb.search(slug, "what the product does and costs", category="product_fact", k=6)]
    from gtm.writer import section
    playbook = {n: section(n, "replies.md") for n in ("Diagnose the objection", "Drafting a reply", "Referrals",
                                                       "Automated disclosure")}
    for r in conn.execute("SELECT * FROM replies WHERE status = 'new' ORDER BY received_at").fetchall():
        try:
            cls = session.json("classify_reply", {"product": cfg.product.name}, ReplyClass,
                               untrusted_blocks={"reply": r["body_stripped"]})
        except (llm.CallCapReached, llm.UsageLimit):
            break
        if cls is None:
            conn.execute("UPDATE replies SET status = 'needs_human' WHERE id = ?", (r["id"],))
            out["needs_human"] += 1
            continue
        conn.execute("UPDATE replies SET intent = ?, classifier_json = ?, status = 'handled' WHERE id = ?",
                     (cls.intent, cls.model_dump_json(), r["id"]))
        out["classified"] += 1
        seq = conn.execute("SELECT * FROM sequences WHERE seq_id = ?", (r["seq_id"],)).fetchone()
        if cls.intent == "referral" and cls.referral_name:
            out["referrals"] += _referral(conn, slug, seq, cls)
        if cls.intent in DRAFT_INTENTS or cls.asks_if_automated:
            try:
                d = session.json("draft_reply", {"classification": cls.model_dump(), "facts": facts,
                                                 "playbook": playbook, "founder": cfg.voice.sender_name},
                                 ReplyDraftOut, untrusted_blocks={"their_reply": r["body_stripped"]})
            except (llm.CallCapReached, llm.UsageLimit):
                break
            if d:
                did = sha1(r["id"], "draft")[:16]
                conn.execute("INSERT OR IGNORE INTO reply_drafts (id, reply_id, recipient_email, mailbox_id, body, content_hash, "
                             "status, updated_at, created_at) VALUES (?,?,?,?,?,?,'pending_review',?,?)",
                             (did, r["id"], seq["recipient_email"], seq["mailbox_id"], d.body.strip(),
                              draft_hash(did, r["id"], seq["recipient_email"], seq["mailbox_id"], d.body.strip()),
                              now_iso(), now_iso()))
                out["drafts"] += 1
    out["proposals"] = propose(conn, led, slug)
    from gtm.report import prune_old_files
    out["pruned_files"] = prune_old_files(slug)
    return out


def _referral(conn, slug: str, seq, cls: ReplyClass) -> int:
    """A person named in a reply becomes a referral lead (it may override the 90-day closure)."""
    lead = conn.execute("SELECT * FROM leads WHERE lead_key = ?", (seq["lead_key"],)).fetchone()
    name = cls.referral_name.strip()[:120]
    lk = lead_key(slug, linkedin_url=None, name=name, domain=lead["account_domain"])
    cur = conn.execute("INSERT OR IGNORE INTO leads (lead_key, account_domain, full_name, first_name, title, referral, "
                       "status, created_at) VALUES (?,?,?,?,?,1,'new',?)",
                       (lk, lead["account_domain"], name, name.split(" ")[0], cls.referral_title, now_iso()))
    return cur.rowcount


def propose(conn, led, slug: str) -> int:
    """Segment-level proposals only past the evidence threshold; each needs the founder's OK."""
    stats = outcomes(conn, led, slug)
    made = 0
    for seg, s in stats["by_segment"].items():
        if s["sent"] < MIN_SENDS or s["positive"] < MIN_POSITIVE:
            continue
        body = {"segment": seg, "stats": s, "note": "segment performs at this rate; review ICP weights"}
        key = sha1(slug, seg, str(s["sent"] // 50))
        if not conn.execute("SELECT 1 FROM proposals WHERE body_json LIKE ?", (f'%"{key}"%',)).fetchone():
            conn.execute("INSERT INTO proposals (kind, body_json, at) VALUES ('segment', ?, ?)",
                         (json.dumps({**body, "key": key}), now_iso()))
            text = (f"Segment {seg}: {s['positive_per_100']} positive replies per 100 delivered "
                    f"(95% CI {s['ci95_per_100'][0]}-{s['ci95_per_100'][1]}, n={s['delivered']})")
            kb.add(slug, "learning", text)
            path = product_config_dir(slug) / "learnings.jsonl"  # so `gtm kb rebuild` keeps it
            with path.open("a") as f:
                f.write(json.dumps({"text": text, "at": now_iso()}) + "\n")
            made += 1
    return made + _propose_engine(conn, led, slug)


def _propose_engine(conn, led, slug: str) -> int:
    """One discovery engine clearly ahead (non-overlapping 95% ranges, past the same evidence bar):
    suggest shifting effort towards it. Only a suggestion; nothing changes without the founder."""
    from gtm.report import engine_funnel
    ef = engine_funnel(conn, led, slug)
    best = ef["leader"]
    if not best or ef["engines"][best]["delivered"] < MIN_SENDS or ef["engines"][best]["positive"] < MIN_POSITIVE:
        return 0
    key = sha1(slug, "engine", best, str(ef["engines"][best]["delivered"] // 50))
    if conn.execute("SELECT 1 FROM proposals WHERE body_json LIKE ?", (f'%"{key}"%',)).fetchone():
        return 0
    stats = {e: r for e, r in ef["engines"].items() if not r["too_few_to_tell"]}
    conn.execute("INSERT INTO proposals (kind, body_json, at) VALUES ('engine', ?, ?)",
                 (json.dumps({"engine": best, "stats": stats, "key": key,
                              "note": f"companies found by {best} reply best; consider shifting effort towards it"}),
                  now_iso()))
    return 1
