"""Outbox -> Snov (plan §5 Phase 7; Harvey's ladder plus hash-bound signed approvals).

For each approved sequence:
 1. recompute the content hash from the CURRENT rendered emails; find an approval whose hash
    matches and whose signature verifies (tty_pin) or that a signed auto-send record covers (policy)
 2. atomic claim: UPDATE ... SET status='claimed' WHERE status='approved' AND content_hash=?
 3. re-run every gate after the claim (suppression, freshness, geo, mailbox ready/ramp/bounce,
    kill switch) and, in ONE BEGIN IMMEDIATE on the ledger, the account-navigation check + touch
    reservations + the active ledger sequence
 4. push; an exception after the request may have gone out -> failed_maybe_sent, never retried
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from gtm import gates, ledger, mailboxes, sequence, signing
from gtm.observations import latest
from gtm.util import now, now_iso

STUCK_CLAIM_MIN = 15


class PushRefused(Exception):
    pass


def approval_for(conn: sqlite3.Connection, ledger_conn: sqlite3.Connection, *, product: str, target: str,
                 target_id: str, content_hash: str, holdout: bool = False, score: float | None = None,
                 auto_min: float = 85, edited: bool = False) -> sqlite3.Row | None:
    """The approval that authorises exactly this content, or None."""
    rows = conn.execute("SELECT * FROM approvals WHERE target = ? AND target_id = ? AND content_hash = ? "
                        "ORDER BY id DESC", (target, target_id, content_hash)).fetchall()
    for r in rows:
        if r["via"] == "tty_pin":
            payload = signing.approval_payload(
                product=product, target=target, target_id=target_id, content_hash=r["content_hash"],
                approved_by=r["approved_by"], approved_at=r["approved_at"], via="tty_pin",
                edited=bool(r["edited"]))
            if signing.verify(payload, r["signature"]):
                return r
        elif r["via"] == "policy" and target == "sequence":
            # auto-send never covers content a human edited (that needs the founder's own signature)
            if (not holdout and not edited and not r["edited"] and r["approved_by"] == "policy:autosend"
                    and score is not None and score >= auto_min and autosend_active(ledger_conn, product)):
                return r
    return None


def autosend_active(ledger_conn: sqlite3.Connection, product: str) -> bool:
    row = ledger_conn.execute("SELECT * FROM autosend WHERE product = ?", (product,)).fetchone()
    if not row or not row["enabled"]:
        return False
    payload = json.loads(row["signature"]).get("payload") if row["signature"] and row["signature"].startswith("{") else None
    if not payload:
        return False
    return signing.verify(payload, json.loads(row["signature"])["sig"]) and payload.get("product") == product


def guardrails_red(ledger_conn: sqlite3.Connection, product: str) -> str | None:
    """Plan §6.3: auto-send turns itself off when bounces or complaints appear after it was enabled."""
    sent = ledger_conn.execute("SELECT count(*) FROM account_sequences WHERE product = ?", (product,)).fetchone()[0]
    bounced = ledger_conn.execute("SELECT count(*) FROM account_sequences WHERE product = ? AND status = 'bounced'",
                                  (product,)).fetchone()[0]
    if sent and bounced / sent >= 0.02:
        return f"bounce rate {bounced}/{sent}"
    if ledger_conn.execute("SELECT 1 FROM suppression WHERE reason = 'complaint' LIMIT 1").fetchone():
        return "a spam complaint"
    return None


def in_window(cfg) -> bool:
    tz = ZoneInfo(cfg.schedule.timezone)
    hm = lambda s: tuple(int(x) for x in str(s).split(":")[:2])  # "9:00" and "09:00" alike
    t = datetime.now(tz)
    return hm(cfg.sending.window.get("start", "00:00")) <= (t.hour, t.minute) < hm(cfg.sending.window.get("end", "23:59"))


def _release_stuck(ctx) -> None:
    cutoff = (now() - timedelta(minutes=STUCK_CLAIM_MIN)).isoformat(timespec="seconds")
    cur = ctx.conn.execute("UPDATE sequences SET status = 'failed_maybe_sent', updated_at = ? "
                           "WHERE status = 'claimed' AND claimed_at < ?", (now_iso(), cutoff))
    if cur.rowcount:
        ctx.note(f"{cur.rowcount} sequence(s) stuck in 'claimed' > {STUCK_CLAIM_MIN} min -> failed_maybe_sent "
                 "(check Snov by hand; never retried automatically)")


def _autosend_policy(ctx) -> None:
    """With a signed auto-send record, write policy approvals for validated, gated, high-score leads.
    Holdout leads are never auto-approved."""
    if not autosend_active(ctx.ledger, ctx.slug):
        return
    red = guardrails_red(ctx.ledger, ctx.slug)
    if red:
        ctx.ledger.execute("UPDATE autosend SET enabled = 0 WHERE product = ?", (ctx.slug,))
        ctx.note(f"auto-send switched OFF ({red}); every email needs your review again")
        return
    rows = ctx.conn.execute("SELECT * FROM sequences WHERE status = 'pending_review' AND holdout = 0 "
                            "AND score >= ? AND edited = 0", (ctx.cfg.sending.auto_send_min_score,)).fetchall()
    for seq in rows:
        if json.loads(seq["validator_json"] or "[]"):
            continue
        ctx.conn.execute("INSERT INTO approvals (target, target_id, content_hash, approved_by, approved_at, "
                         "via, edited, signature) VALUES ('sequence', ?, ?, ?, ?, 'policy', 0, NULL)",
                         (seq["seq_id"], seq["content_hash"], "policy:autosend", now_iso()))
        ctx.conn.execute("UPDATE sequences SET status = 'approved', updated_at = ? WHERE seq_id = ? "
                         "AND status = 'pending_review'", (now_iso(), seq["seq_id"]))
        ctx.count("autosend_approved")


def drain(ctx) -> None:
    _release_stuck(ctx)
    if ledger.sending_paused(ctx.ledger):
        ctx.note("sending is paused (global kill switch): nothing pushed")
        return
    if not in_window(ctx.cfg):
        ctx.note("outside the sending window: nothing pushed")
        return
    _autosend_policy(ctx)
    from gtm.sources import snov
    client = None
    for seq in ctx.conn.execute("SELECT * FROM sequences WHERE status = 'approved' ORDER BY score DESC").fetchall():
        try:
            client = client or snov.client()
            push_one(ctx, seq, client)
            ctx.count("pushed")
        except PushRefused as e:
            ctx.count("push_refused")
            ctx.note(f"{seq['seq_id']}: not pushed: {e}")


def _gate_after_claim(ctx, seq, lead, account) -> str | None:
    obs = latest(ctx.conn, account["domain"])
    score = seq["score"]
    mb = mailboxes.get(ctx.ledger, seq["mailbox_id"])
    if mb is None or not mailboxes.signed_ok(mb):
        return "mailbox missing or its limits are not PIN-signed"
    if mb["paused"]:
        return f"mailbox {mb['id']} is paused"
    if mb["ready_state"] != "ready":
        return f"mailbox {mb['id']} domain is NOT READY (run gtm warmup)"
    res = gates.check_lead(cfg=ctx.cfg, conn=ctx.conn, ledger_conn=ctx.ledger, lead=lead, account=account,
                           observations=obs, score_total=score,
                           mailbox_catchall_allowed=bool(mb["catchall_allowed"]),
                           catchall_share_ok=_catchall_share_ok(ctx))
    if not res.ok:
        return "; ".join(res.failures)
    if (res.notice or None) != (seq["notice"] or None):
        return "geo notice changed since writing: rewrite needed"
    if ledger.sending_paused(ctx.ledger):
        return "kill switch engaged"
    return None


def _catchall_share_ok(ctx) -> bool:
    rows = ctx.ledger.execute("SELECT email_class FROM account_sequences WHERE product = ? AND started_at >= ?",
                              (ctx.slug, (now() - timedelta(days=30)).isoformat())).fetchall()
    if not rows:
        return False
    return sum(r[0] == "catchall" for r in rows) / len(rows) < 0.2


def push_one(ctx, seq: sqlite3.Row, client) -> None:
    lead = ctx.conn.execute("SELECT * FROM leads WHERE lead_key = ?", (seq["lead_key"],)).fetchone()
    account = ctx.conn.execute("SELECT * FROM accounts WHERE domain = ?", (lead["account_domain"],)).fetchone()
    steps = sequence.steps_from_json(seq["steps_json"])
    vctx = sequence.context(ctx.cfg, seq=seq, lead=lead, account=account, notice=seq["notice"])
    current = sequence.content_hash(seq_id=seq["seq_id"], lead_key=seq["lead_key"],
                                    recipient_email=lead["email"] or "", mailbox_id=seq["mailbox_id"],
                                    steps=steps, ctx=vctx)
    if current != seq["content_hash"]:
        raise PushRefused("content changed since it was written/approved (hash mismatch)")
    if seq["recipient_email"].lower() != (lead["email"] or "").lower():
        raise PushRefused("recipient differs from the lead record")
    if approval_for(ctx.conn, ctx.ledger, product=ctx.slug, target="sequence", target_id=seq["seq_id"],
                    content_hash=current, holdout=bool(seq["holdout"]), score=seq["score"],
                    auto_min=ctx.cfg.sending.auto_send_min_score, edited=bool(seq["edited"])) is None:
        raise PushRefused("no valid signed approval for this exact content")
    from gtm import validate
    violations = validate.validate_sequence(steps, vctx)
    if violations:
        raise PushRefused("validator: " + "; ".join(f"{v.rule}" for v in violations[:5]))

    claimed = ctx.conn.execute("UPDATE sequences SET status = 'claimed', claimed_at = ?, updated_at = ? "
                               "WHERE seq_id = ? AND status = 'approved' AND content_hash = ?",
                               (now_iso(), now_iso(), seq["seq_id"], current)).rowcount
    if claimed != 1:
        raise PushRefused("already claimed by another process")

    problem = _gate_after_claim(ctx, seq, lead, account)
    camp = ctx.ledger.execute("SELECT * FROM snov_campaigns WHERE product = ? AND mailbox_id = ?",
                              (ctx.slug, seq["mailbox_id"])).fetchone()
    if not problem and camp is None:
        problem = f"no Snov campaign linked for mailbox {seq['mailbox_id']} (gtm snov link)"
    if not problem and not camp["settings_ok"]:
        problem = "Snov campaign settings not verified (tracking off, stop on company reply): gtm snov link"
    if problem:
        # mailbox/campaign/kill-switch problems pass; a suppressed, stale or blocked lead doesn't
        transient = any(k in problem for k in ("mailbox", "kill switch", "Snov campaign"))
        _unclaim(ctx, seq, "approved" if transient else "cancelled")
        raise PushRefused(problem + ("" if transient else " (sequence cancelled)"))

    email_hash = ledger.email_hash(lead["email"])
    ctx.ledger.execute("BEGIN IMMEDIATE")
    try:
        ok, why = gates.navigation_ok(ctx.ledger, product=ctx.slug, domain=account["domain"],
                                      lead_key=lead["lead_key"], referral=bool(lead["referral"]),
                                      recontact_days=ctx.cfg.scoring.recontact_days)
        if ok:
            ok, why = mailboxes.reserve(ctx.ledger, mailbox_id=seq["mailbox_id"], seq_id=seq["seq_id"],
                                        days=mailboxes.touch_days(ctx.cfg.sending.followup_days))
        if not ok:
            ctx.ledger.execute("ROLLBACK")
            _unclaim(ctx, seq, "approved")
            raise PushRefused(why)
        ctx.ledger.execute(
            "INSERT INTO account_sequences (id, product, lead_key, email_hash, domain, mailbox_id, status, "
            "step_sent, referral, started_at, email_class) VALUES (?,?,?,?,?,?,'active',0,?,?,?)",
            (seq["seq_id"], ctx.slug, lead["lead_key"], email_hash, account["domain"], seq["mailbox_id"],
             int(lead["referral"]), now_iso(), lead["email_class"]))
        ctx.ledger.execute("COMMIT")
    except PushRefused:
        raise
    except Exception:
        ctx.ledger.execute("ROLLBACK")
        _unclaim(ctx, seq, "approved")
        raise

    first, _, last = (lead["full_name"] or "").partition(" ")
    try:
        client.add_prospect(int(camp["list_id"]), email=lead["email"], first_name=first, last_name=last,
                            company=account["name"], position=lead["title"],
                            custom_fields=sequence.custom_fields(steps, vctx))
    except Exception as e:
        # the request may have reached Snov: never retry automatically (Harvey's rule)
        ctx.conn.execute("UPDATE sequences SET status = 'failed_maybe_sent', updated_at = ? WHERE seq_id = ?",
                         (now_iso(), seq["seq_id"]))
        ctx.ledger.execute("INSERT INTO manual_actions (product, action, detail, created_at) VALUES (?,?,?,?)",
                           (ctx.slug, "check_snov_push", f"sequence {seq['seq_id']}: push raised {type(e).__name__}; "
                            "check the Snov list before doing anything", now_iso()))
        raise PushRefused(f"push error, marked failed_maybe_sent: {type(e).__name__}") from e
    ctx.conn.execute("UPDATE sequences SET status = 'pushed', pushed_at = ?, updated_at = ? WHERE seq_id = ?",
                     (now_iso(), now_iso(), seq["seq_id"]))
    ctx.conn.execute("UPDATE leads SET status = 'contacted' WHERE lead_key = ?", (lead["lead_key"],))


def _unclaim(ctx, seq, to_status: str) -> None:
    ctx.conn.execute("UPDATE sequences SET status = ?, claimed_at = NULL, updated_at = ? "
                     "WHERE seq_id = ? AND status = 'claimed'", (to_status, now_iso(), seq["seq_id"]))
