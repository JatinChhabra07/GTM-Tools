"""Writer (plan §5 Phase 7): FACTS-only prompt -> 3-step sequence -> humanize -> validator.

The prompt is built from the lead's observations (ONE primary signal by rank), one proof and the
product facts, plus only the playbook sections this lead needs (Harvey concatenated ~900 lines
into every call). `reference_policy` is decided here, in code. Each draft is validated by
gtm/validate.py; a draft that breaks a rule goes back with the violations, at most 3 attempts.
"""

from __future__ import annotations

import json
import re
import sqlite3

from gtm import gates, kb, sequence, validate
from gtm import observations as obs
from gtm.paths import REPO_ROOT
from gtm.schemas import HumanizeOut, SequenceOut
from gtm.util import now_iso, sha1

MAX_ATTEMPTS = 3
WRITING_SECTIONS = ("Core principles", "Length and shape", "Email 1: the observation", "Email 2: a new angle",
                    "Email 3: the breakup", "Subject lines", "Reference policy", "Personalisation that works",
                    "Truthful identity")
SIGNAL_SECTION = {"ASKS_FOR_SOLUTION": "Stated intent", "COMPETITOR_PAIN": "Stated intent", "SWITCHING": "Stated intent",
                  "HIRING_FOR_PROBLEM": "Hiring", "HIRING_ROLE": "Hiring", "FUNDING": "Funding",
                  "LEADERSHIP_CHANGE": "Leadership change", "TECH_ADDED": "Tech change and tech present",
                  "TECH_REMOVED": "Tech change and tech present", "RUNNING_GOOGLE_ADS": "Ad spend",
                  "RUNNING_META_ADS": "Ad spend"}
_MERGE = re.compile(r"\{\{[^}]*\}\}|\{[^}]*\}|\[[A-Z_]+\]|%[a-z_]+%")
_META = ("here is", "here's", "sure", "i can't", "i cannot", "as an ai", "sorry")


def section(name: str, file: str) -> str:
    text = (REPO_ROOT / "playbooks" / file).read_text()
    m = re.search(rf"^## {re.escape(name)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def reference_policy(primary, lead: sqlite3.Row, post: sqlite3.Row | None) -> str:
    if primary.kind == "intent" and lead["is_signal_author"] and post is not None and post["source"] in ("linkedin", "x") \
            and post["resolvability"] == "named_person":
        return "self_public_professional"
    if primary.kind == "trigger" or (post is not None and post["resolvability"] == "company_event"):
        return "company_event"
    return "theme_only"


def pick_mailbox(ctx) -> str:
    shares = ctx.cfg.sending.mailboxes
    if not shares:
        return "none"
    today = now_iso()[:10]
    def load(mb):
        n = ctx.conn.execute("SELECT count(*) FROM sequences WHERE mailbox_id = ? AND created_at >= ?",
                             (mb.id, today)).fetchone()[0]
        return n / mb.share
    return min(shares, key=load).id


def run(ctx) -> None:
    rows = ctx.conn.execute(
        "SELECT l.* FROM leads l WHERE l.status = 'qualified' AND NOT EXISTS (SELECT 1 FROM sequences s WHERE "
        "s.lead_key = l.lead_key AND s.status NOT IN ('rejected','cancelled'))").fetchall()
    for lead in rows:
        if ctx.llm.remaining < 2:
            ctx.note("isolated-call budget used up: remaining qualified leads are written next run")
            return
        write_for(ctx, lead)


def _humanize_on(ctx) -> bool:
    h = ctx.cfg.voice.humanize
    return h == "on" or (h == "auto" and ctx.cfg.usage.profile == "max20x")


def build_inputs(ctx, lead: sqlite3.Row, account: sqlite3.Row) -> dict | None:
    score = ctx.conn.execute("SELECT * FROM scores WHERE lead_key = ? ORDER BY created_at DESC LIMIT 1",
                             (lead["lead_key"],)).fetchone()
    latest = [o for o in obs.latest(ctx.conn, account["domain"], kinds=("intent", "trigger")) if o.fresh]
    if not latest:
        return None
    own = [o for o in latest if o.lead_key in (None, lead["lead_key"])]
    primary = sorted(own or latest, key=lambda o: (o.rank, -o.confidence))[0]
    post = ctx.conn.execute("SELECT * FROM posts WHERE url = ?", (primary.evidence_url,)).fetchone()
    policy = reference_policy(primary, lead, post)
    q = f"{primary.signal_code.lower().replace('_', ' ')} {primary.value_text or ''} {lead['persona'] or ''}"
    proofs = kb.search(ctx.slug, q, category="proof", k=2)
    facts = kb.search(ctx.slug, q, category="product_fact", k=5)
    voice = kb.search(ctx.slug, "tone and style", category="voice", k=2)
    pains = kb.search(ctx.slug, q, category="pain_language", k=3)
    notes = [r["note"] for r in ctx.conn.execute(
        "SELECT note FROM writer_notes WHERE scope = 'campaign' OR (scope = 'contact' AND lead_key = ?) "
        "ORDER BY id DESC LIMIT 8", (lead["lead_key"],))]
    proof = proofs[0] if proofs else None
    signal_texts = [primary.value_text or ""] + ([post["text_excerpt"]] if post else [])
    ok, _, notice = gates.geo_allowed(ctx.cfg, lead_country=lead["country"], hq_country=account["hq_country"],
                                      domain=account["domain"])
    return {
        "primary": primary, "policy": policy, "post": post, "proof": proof, "facts": facts, "notice": notice,
        "payload": {
            "product": {"name": ctx.cfg.product.name, "url": str(ctx.cfg.product.url)},
            "facts": [f["text"] for f in facts],
            "proof": {"id": proof["id"], "text": proof["text"]} if proof else None,
            "voice": [v["text"] for v in voice] + [ctx.cfg.voice.tone],
            "recipient": {"first_name": lead["first_name"] or "", "title": lead["title"] or "",
                          "company": account["name"], "greeting": (lead["first_name"] or f"{account['name']} team"),
                          "is_generic_inbox": bool(lead["is_generic_inbox"])},
            "primary_signal": {"id": primary.id, "code": primary.signal_code, "kind": primary.kind,
                               "playbook": section(SIGNAL_SECTION.get(primary.signal_code, "No real signal"), "signals.md")},
            "reference_policy": policy,
            "playbook": {name: section(name, "writing.md") for name in WRITING_SECTIONS},
            "founder_notes": notes,
            "allowed_link": str(ctx.cfg.product.url),
        },
        "untrusted": {"signal_from_web": "\n".join(t for t in signal_texts if t)[:1200],
                      **({"pain_language_from_web": "\n".join(p["text"] for p in pains)} if pains else {})},
        "context_json": {
            "allowed_urls": [str(ctx.cfg.product.url)],
            "proof_quotes": [p["text"] for p in proofs[:1]] + [f["text"] for f in facts],
            "claimable_names": [r[0] for r in ctx.conn.execute("SELECT name FROM clients")] + list(ctx.cfg.signals.competitors),
            "signal_texts": [t for t in signal_texts if t],
            "score_snapshot": json.loads(score["feature_snapshot_json"]) if score else {},
        },
        "score": score["total"] if score else None, "holdout": bool(score["holdout"]) if score else False,
    }


def write_for(ctx, lead: sqlite3.Row) -> str | None:
    account = ctx.conn.execute("SELECT * FROM accounts WHERE domain = ?", (lead["account_domain"],)).fetchone()
    inp = build_inputs(ctx, lead, account)
    if inp is None:
        ctx.conn.execute("UPDATE leads SET status = 'dropped' WHERE lead_key = ?", (lead["lead_key"],))
        ctx.count("dropped_stale_before_write")
        return None
    seq_id = sha1(lead["lead_key"], ctx.run_id)[:16]
    mailbox = pick_mailbox(ctx)
    row = {"seq_id": seq_id, "recipient_email": lead["email"], "reference_policy": inp["policy"],
           "context_json": json.dumps(inp["context_json"])}
    vctx = sequence.context(ctx.cfg, seq=row, lead=lead, account=account, notice=inp["notice"])
    steps, violations, attempts, feedback = None, [], 0, None
    while attempts < MAX_ATTEMPTS:
        payload = dict(inp["payload"])
        if feedback:
            payload["validator_feedback"] = feedback
        out = ctx.llm.json("write_sequence", payload, SequenceOut, untrusted_blocks=inp["untrusted"])
        attempts += 1
        if out is None:
            continue
        draft = [validate.Step(s.step, s.subject.strip() if s.step == 1 else "", s.body.strip())
                 for s in sorted(out.steps, key=lambda s: s.step)]
        violations = validate.validate_sequence(draft, vctx)
        if not violations:
            steps = draft
            break
        steps = draft
        feedback = [f"step {v.step}: {v.rule}: {v.message}" for v in violations]
    if steps is None:
        ctx.count("write_failed")
        return None
    if not violations and _humanize_on(ctx) and ctx.llm.remaining >= 1:
        steps = _humanize(ctx, steps, vctx)
    content_hash = sequence.content_hash(seq_id=seq_id, lead_key=lead["lead_key"], recipient_email=lead["email"],
                                         mailbox_id=mailbox, steps=steps, ctx=vctx)
    ctx.conn.execute(
        "INSERT INTO sequences (seq_id, lead_key, steps_json, facts_used_json, reference_policy, proof_ids_json, "
        "recipient_email, mailbox_id, content_hash, status, writer_attempts, updated_at, created_at, score, holdout, "
        "validator_json, context_json, notice) VALUES (?,?,?,?,?,?,?,?,?,'pending_review',?,?,?,?,?,?,?,?)",
        (seq_id, lead["lead_key"], sequence.steps_to_json(steps), json.dumps([inp["primary"].id]), inp["policy"],
         json.dumps([inp["proof"]["id"]] if inp["proof"] else []), lead["email"], mailbox, content_hash, attempts,
         now_iso(), now_iso(), inp["score"], int(inp["holdout"]),
         json.dumps([{"rule": v.rule, "step": v.step, "message": v.message} for v in violations]),
         json.dumps(inp["context_json"]), inp["notice"]))
    ctx.conn.execute("UPDATE leads SET status = 'sequenced' WHERE lead_key = ?", (lead["lead_key"],))
    ctx.count("sequences_written" if not violations else "sequences_need_edit")
    return seq_id


def _words(text: str) -> int:
    return len(text.split())


def accept_humanized(before: list[validate.Step], after: list[validate.Step]) -> bool:
    """Harvey's acceptance checks: same merge vars, no dash added, <= +2 words, no meta-text."""
    if len(before) != len(after):
        return False
    for b, a in zip(before, after):
        if sorted(_MERGE.findall(b.body + b.subject)) != sorted(_MERGE.findall(a.body + a.subject)):
            return False
        if ("—" in a.body or "–" in a.body) and not ("—" in b.body or "–" in b.body):
            return False
        if _words(a.body) > _words(b.body) + 2:
            return False
        if a.body.strip().lower().startswith(_META):
            return False
    return True


def _humanize(ctx, steps: list[validate.Step], vctx: validate.Context) -> list[validate.Step]:
    out = ctx.llm.json("humanize", {"steps": [{"step": s.step, "subject": s.subject, "body": s.body} for s in steps]},
                       HumanizeOut)
    if out is None:
        return steps
    after = [validate.Step(s.step, s.subject.strip() if s.step == 1 else "", s.body.strip())
             for s in sorted(out.steps, key=lambda s: s.step)]
    if accept_humanized(steps, after) and not validate.validate_sequence(after, vctx):
        ctx.count("humanized")
        return after
    return steps
