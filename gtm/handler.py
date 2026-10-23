"""Replies, bounces, unsubscribes (plan §5 Phase 7 safety part + Phase 9 conversations).

`sync` is step 1 of every run and of the hourly sync. It is deterministic on purpose: opt-out
keywords and bounces act BEFORE any model sees a reply, and if the pull fails nothing is pushed.
`feedback` (daily) classifies replies in isolated calls and drafts answers for `gtm review`.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import timedelta

from gtm import ledger, mailboxes, validate
from gtm.util import normalize_text, now, now_iso, sha1

ESCALATE_WORDS = ("lawyer", "attorney", "legal action", "legal team", "sue ", "lawsuit", "gdpr", "dpdp",
                  "report you", "reported you", "harass", "cease and desist", "contract", "invoice",
                  "breach", "complaint", "police", "fraud", "scam")
EXTRA_OPT_OUT = ("please remove", "leave me alone", "stop emailing", "stop contacting", "not relevant",
                 "wrong person", "spam")
_QUOTE_HEADERS = (re.compile(r"^on .{0,200}wrote:\s*$", re.I), re.compile(r"^-{2,}\s*original message", re.I),
                  re.compile(r"^from:\s", re.I), re.compile(r"^sent from my ", re.I),
                  re.compile(r"^_{5,}"))


def strip_reply(text: str, ours: list[str]) -> str:
    """Keep only what the person wrote: drop quoted history, our own sent text and footer lines."""
    our_lines = {normalize_text(line) for t in ours for line in t.splitlines() if line.strip()}
    kept = []
    for line in (text or "").replace("\r", "").splitlines():
        s = line.strip()
        if any(p.match(s) for p in _QUOTE_HEADERS):
            break
        if s.startswith(">") or (s and normalize_text(s) in our_lines):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def is_opt_out(stripped: str) -> bool:
    return validate.is_opt_out_phrase(stripped) or bool(validate.find_phrases(stripped, EXTRA_OPT_OUT))


def needs_escalation(stripped: str) -> bool:
    low = f" {stripped.lower()} "
    return any(w in low for w in ESCALATE_WORDS)


# ---------------------------------------------------------------- suppression helpers

def suppress(ledger_conn: sqlite3.Connection, *, email: str, reason: str, domain: str | None = None) -> None:
    h = ledger.email_hash(email)
    exists = ledger_conn.execute("SELECT 1 FROM suppression WHERE email_hash = ? AND reason = ?",
                                 (h, reason)).fetchone()
    if not exists:
        ledger_conn.execute("INSERT INTO suppression (email_hash, domain, reason, added_at) VALUES (?,?,?,?)",
                            (h, domain, reason, now_iso()))


def end_sequence(ledger_conn: sqlite3.Connection, seq_id: str, status: str) -> None:
    ledger_conn.execute("UPDATE account_sequences SET status = ?, ended_at = ? WHERE id = ? AND status = 'active'",
                        (status, now_iso(), seq_id))


def stop_everywhere(ledger_conn: sqlite3.Connection, client, email: str) -> list[str]:
    """New suppression: Snov do-not-email list + Unsubscribed in every campaign we own (all products).
    Returns problems (also queued as manual actions)."""
    problems = []
    dne = ledger_conn.execute("SELECT value FROM settings WHERE key = 'snov_dne_list_id'").fetchone()
    try:
        if dne:
            client.blacklist_add(int(dne[0]), [email])
        else:
            problems.append("no Snov do-not-email list linked")
    except Exception as e:
        problems.append(f"do-not-email add failed: {type(e).__name__}")
    for camp in ledger_conn.execute("SELECT campaign_id FROM snov_campaigns").fetchall():
        try:
            client.set_recipient_status(int(camp[0]), email, "Unsubscribed")
        except Exception:
            pass  # not in that campaign, or already finished: the DNE list still blocks it
    for p in problems:
        ledger_conn.execute("INSERT INTO manual_actions (action, detail, created_at) VALUES (?,?,?)",
                            ("snov_suppress", f"{p}: add {ledger.email_hash(email)[:12]}… to Snov DNE by hand", now_iso()))
    return problems


def pause_campaigns(ledger_conn: sqlite3.Connection, client, *, mailbox_id: str | None = None,
                    product: str | None = None) -> list[str]:
    """Pausing also stops Snov's scheduled follow-ups. What the API can't pause goes to manual actions."""
    sql, args = "SELECT * FROM snov_campaigns WHERE 1=1", []
    if mailbox_id:
        sql += " AND mailbox_id = ?"
        args.append(mailbox_id)
    if product:
        sql += " AND product = ?"
        args.append(product)
    failed = []
    for camp in ledger_conn.execute(sql, args).fetchall():
        try:
            client.campaign_action(int(camp["campaign_id"]), "pause")
        except Exception as e:
            failed.append(str(camp["campaign_id"]))
            ledger_conn.execute("INSERT INTO manual_actions (product, action, detail, created_at) VALUES (?,?,?,?)",
                                (camp["product"], "pause_campaign",
                                 f"pause Snov campaign {camp['campaign_id']} by hand ({type(e).__name__})", now_iso()))
    return failed


# ---------------------------------------------------------------- sync (step 1)

def sync(ctx) -> None:
    camps = ctx.ledger.execute("SELECT * FROM snov_campaigns WHERE product = ?", (ctx.slug,)).fetchall()
    if not camps:
        ctx.sync_ok = True  # nothing was ever pushed for this product: nothing to read
        return
    from gtm.sources import snov
    client = snov.client()
    seqs = {r["recipient_email"].lower(): r for r in ctx.conn.execute(
        "SELECT * FROM sequences WHERE status IN ('pushed','failed_maybe_sent')").fetchall()}
    wm = ctx.conn.execute("SELECT last_collected_at FROM watermarks WHERE source = 'snov_activity'").fetchone()
    date_from = (wm[0][:10] if wm else (now() - timedelta(days=30)).date().isoformat())
    floor = (now() - timedelta(days=30)).date().isoformat()  # Snov caps the window at 31 days
    date_from = max(date_from, floor)
    date_to = now().date().isoformat()
    for camp in camps:
        _replies(ctx, client, camp, seqs)
        _activity(ctx, client, camp, seqs, date_from, date_to)
        for email in client.finished(int(camp["campaign_id"])):
            if email in seqs:
                end_sequence(ctx.ledger, seqs[email]["seq_id"], "finished")
    ctx.conn.execute("INSERT INTO watermarks (source, last_collected_at) VALUES ('snov_activity', ?) "
                     "ON CONFLICT(source) DO UPDATE SET last_collected_at = excluded.last_collected_at",
                     (now_iso(),))
    ctx.sync_ok = True


def _our_texts(seq: sqlite3.Row) -> list[str]:
    from gtm.sequence import steps_from_json
    return [s.body for s in steps_from_json(seq["steps_json"])]


def _replies(ctx, client, camp, seqs) -> None:
    for rec in client.replies(int(camp["campaign_id"])):
        email = (rec.get("prospectEmail") or "").lower()
        seq = seqs.get(email)
        if not seq or rec.get("replyType", "email") != "email":
            continue
        for msg in rec.get("replies") or []:
            rid = sha1(str(camp["campaign_id"]), email, str(msg.get("receivedAt")), (msg.get("subject") or "")[:60])
            if ctx.conn.execute("SELECT 1 FROM replies WHERE id = ?", (rid,)).fetchone():
                continue
            stripped = strip_reply(msg.get("message") or "", _our_texts(seq) + [ctx.cfg.voice.opt_out_line,
                                                                              ctx.cfg.voice.postal_address])
            status, intent, override = "new", None, None
            if is_opt_out(stripped):
                status, intent, override = "handled", "opt_out", "opt_out"
                suppress(ctx.ledger, email=email, reason="unsubscribe")
                end_sequence(ctx.ledger, seq["seq_id"], "opted_out")
                stop_everywhere(ctx.ledger, client, email)
                ctx.count("opt_outs")
            else:
                end_sequence(ctx.ledger, seq["seq_id"], "replied")
                if needs_escalation(stripped):
                    status, override = "needs_human", "escalate"
                ctx.count("replies")
            ctx.conn.execute("INSERT INTO replies (id, seq_id, received_at, body_stripped, intent, keyword_override, "
                             "status, lead_key) VALUES (?,?,?,?,?,?,?,?)",
                             (rid, seq["seq_id"], str(msg.get("receivedAt") or now_iso()), stripped[:4000],
                              intent, override, status, seq["lead_key"]))
            ctx.conn.execute("UPDATE leads SET status = 'replied' WHERE lead_key = ?", (seq["lead_key"],))


def _activity(ctx, client, camp, seqs, date_from: str, date_to: str) -> None:
    mailbox_id = camp["mailbox_id"]
    for ev in client.activity(int(camp["campaign_id"]), date_from, date_to):
        email = (ev.get("recipient_email") or "").lower()
        kind = ev.get("event_type")
        seq = seqs.get(email)
        if not seq:
            continue
        h = ledger.email_hash(email)
        at = str(ev.get("event_time") or now_iso())
        domain = email.rsplit("@", 1)[-1]
        if kind == "sent":
            if not ctx.ledger.execute("SELECT 1 FROM contact_log WHERE email_hash = ? AND mailbox_id = ? AND sent_at = ?",
                                      (h, mailbox_id, at)).fetchone():
                row = ctx.ledger.execute("SELECT step_sent, email_class FROM account_sequences WHERE id = ?",
                                         (seq["seq_id"],)).fetchone()
                step = (row["step_sent"] if row else 0) + 1
                ctx.ledger.execute("INSERT INTO contact_log (email_hash, domain, product, mailbox_id, step, "
                                   "email_class, sent_at) VALUES (?,?,?,?,?,?,?)",
                                   (h, domain, ctx.slug, mailbox_id, step, (row["email_class"] if row else None) or "valid", at))
                ctx.ledger.execute("UPDATE account_sequences SET step_sent = ? WHERE id = ?", (step, seq["seq_id"]))
                ctx.count("sent_events")
        elif kind == "bounce":
            suppress(ctx.ledger, email=email, reason="bounce")
            end_sequence(ctx.ledger, seq["seq_id"], "bounced")
            ctx.ledger.execute("INSERT OR IGNORE INTO bounces (mailbox_id, email_hash, at) VALUES (?,?,?)",
                               (mailbox_id, h, at))
            ctx.count("bounces")
            if mailboxes.apply_bounce_brake(ctx.ledger, mailbox_id):
                failed = pause_campaigns(ctx.ledger, client, mailbox_id=mailbox_id)
                ctx.note(f"bounce rate over 3% on {mailbox_id}: mailbox paused"
                         + (f"; pause campaigns {', '.join(failed)} by hand" if failed else " and its Snov campaigns"))
        elif kind == "unsubscribe":
            suppress(ctx.ledger, email=email, reason="unsubscribe")
            end_sequence(ctx.ledger, seq["seq_id"], "opted_out")
            stop_everywhere(ctx.ledger, client, email)
            ctx.count("unsubscribes")
