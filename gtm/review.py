"""`gtm review` (plan §5 Phase 7): the founder's decision desk, in HIS terminal (TTY + PIN).

One sequence at a time with its evidence beside it. a approve · e edit · r reject + note ·
j/k move · q quit. Every approval is an Ed25519 signature over the exact content hash, so a row
written by anything else never sends. No approve-all: `--batch-min-score` shows every subject and
needs the count typed back.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import sqlite3
import subprocess
import tempfile

from gtm import gates, sequence, signing, validate
from gtm import observations as obs
from gtm.config import ProductConfig
from gtm.util import now_iso


class ReviewError(Exception):
    pass


def _rows(conn, seq: sqlite3.Row):
    lead = conn.execute("SELECT * FROM leads WHERE lead_key = ?", (seq["lead_key"],)).fetchone()
    account = conn.execute("SELECT * FROM accounts WHERE domain = ?", (lead["account_domain"],)).fetchone()
    return lead, account


def _vctx(cfg, conn, seq):
    lead, account = _rows(conn, seq)
    return sequence.context(cfg, seq=seq, lead=lead, account=account, notice=seq["notice"]), lead, account


def refresh_mailbox(cfg: ProductConfig, conn, seq: sqlite3.Row) -> sqlite3.Row:
    """A sequence written before any mailbox existed gets one now (still pending, so no approval breaks)."""
    if seq["mailbox_id"] != "none" or not cfg.sending.mailboxes:
        return seq
    from types import SimpleNamespace

    from gtm.writer import pick_mailbox
    mb = pick_mailbox(SimpleNamespace(cfg=cfg, conn=conn))
    vctx, lead, _ = _vctx(cfg, conn, seq)
    steps = sequence.steps_from_json(seq["steps_json"])
    h = sequence.content_hash(seq_id=seq["seq_id"], lead_key=seq["lead_key"], recipient_email=lead["email"],
                              mailbox_id=mb, steps=steps, ctx=vctx)
    conn.execute("UPDATE sequences SET mailbox_id = ?, content_hash = ?, updated_at = ? WHERE seq_id = ? "
                 "AND status = 'pending_review'", (mb, h, now_iso(), seq["seq_id"]))
    return conn.execute("SELECT * FROM sequences WHERE seq_id = ?", (seq["seq_id"],)).fetchone()


def approve(cfg: ProductConfig, conn, slug: str, seq_id: str, private_key, *, approved_by: str | None = None) -> None:
    seq = conn.execute("SELECT * FROM sequences WHERE seq_id = ?", (seq_id,)).fetchone()
    if not seq or seq["status"] != "pending_review":
        raise ReviewError("only a pending sequence can be approved")
    seq = refresh_mailbox(cfg, conn, seq)
    if seq["mailbox_id"] == "none":
        raise ReviewError("no sending mailbox yet: register one (bin/gtm mailbox add) before approving")
    vctx, lead, _ = _vctx(cfg, conn, seq)
    steps = sequence.steps_from_json(seq["steps_json"])
    violations = validate.validate_sequence(steps, vctx)
    if violations:
        raise ReviewError("fix these first (e to edit): " + "; ".join(f"{v.rule}: {v.message}" for v in violations))
    current = sequence.content_hash(seq_id=seq_id, lead_key=seq["lead_key"], recipient_email=lead["email"],
                                    mailbox_id=seq["mailbox_id"], steps=steps, ctx=vctx)
    if current != seq["content_hash"]:
        raise ReviewError("the rendered email changed since it was written (voice/footer/recipient): rewrite or edit it")
    who, at = approved_by or getpass.getuser(), now_iso()
    payload = signing.approval_payload(product=slug, target="sequence", target_id=seq_id, content_hash=current,
                                       approved_by=who, approved_at=at, via="tty_pin", edited=bool(seq["edited"]))
    sig = signing.sign(private_key, payload)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT INTO approvals (target, target_id, content_hash, approved_by, approved_at, via, edited, signature) "
                     "VALUES ('sequence', ?, ?, ?, ?, 'tty_pin', ?, ?)", (seq_id, current, who, at, int(seq["edited"]), sig))
        conn.execute("UPDATE sequences SET status = 'approved', updated_at = ? WHERE seq_id = ? AND status = 'pending_review'",
                     (at, seq_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def reject(conn, seq_id: str, note: str, scope: str = "contact") -> None:
    seq = conn.execute("SELECT * FROM sequences WHERE seq_id = ?", (seq_id,)).fetchone()
    if not seq or seq["status"] != "pending_review":
        raise ReviewError("only a pending sequence can be rejected")
    if note.strip():
        conn.execute("INSERT INTO writer_notes (scope, lead_key, note, at) VALUES (?,?,?,?)",
                     (scope, seq["lead_key"] if scope == "contact" else None, note.strip()[:500], now_iso()))
    notes = json.loads(seq["feedback_notes_json"] or "[]") + ([note.strip()] if note.strip() else [])
    conn.execute("UPDATE sequences SET status = 'rejected', feedback_notes_json = ?, updated_at = ? WHERE seq_id = ?",
                 (json.dumps(notes), now_iso(), seq_id))
    conn.execute("UPDATE leads SET status = 'dropped' WHERE lead_key = ?", (seq["lead_key"],))


def to_text(steps: list[validate.Step]) -> str:
    out = []
    for s in steps:
        out.append(f"=== STEP {s.step} ===")
        if s.step == 1:
            out.append(f"SUBJECT: {s.subject}")
        out.append(s.body)
    return "\n".join(out) + "\n"


def from_text(text: str) -> list[validate.Step]:
    parts = re.split(r"^=== STEP (\d) ===\s*$", text, flags=re.M)
    steps = []
    for i in range(1, len(parts), 2):
        n, body = int(parts[i]), parts[i + 1].strip("\n")
        subject = ""
        m = re.match(r"SUBJECT:\s*(.*)\n?", body)
        if m:
            subject, body = m.group(1).strip(), body[m.end():]
        steps.append(validate.Step(n, subject, body.strip()))
    return steps


def apply_edit(cfg: ProductConfig, conn, seq_id: str, steps: list[validate.Step]) -> list[validate.Violation]:
    """Edits are allowed only while pending. New content = new hash; it stays pending until approved."""
    seq = conn.execute("SELECT * FROM sequences WHERE seq_id = ?", (seq_id,)).fetchone()
    if not seq or seq["status"] != "pending_review":
        raise ReviewError("edits are allowed only on a pending sequence")
    vctx, lead, _ = _vctx(cfg, conn, seq)
    violations = validate.validate_sequence(steps, vctx)
    h = sequence.content_hash(seq_id=seq_id, lead_key=seq["lead_key"], recipient_email=lead["email"],
                              mailbox_id=seq["mailbox_id"], steps=steps, ctx=vctx)
    conn.execute("UPDATE sequences SET steps_json = ?, content_hash = ?, edited = 1, validator_json = ?, updated_at = ? "
                 "WHERE seq_id = ?", (sequence.steps_to_json(steps), h,
                                      json.dumps([{"rule": v.rule, "step": v.step, "message": v.message} for v in violations]),
                                      now_iso(), seq_id))
    return violations


def evidence(cfg, conn, ledger_conn, seq: sqlite3.Row) -> str:
    vctx, lead, account = _vctx(cfg, conn, seq)
    score = conn.execute("SELECT * FROM scores WHERE lead_key = ? ORDER BY created_at DESC LIMIT 1", (seq["lead_key"],)).fetchone()
    lines = [f"{lead['full_name']} · {lead['title'] or '?'} · persona {lead['persona']} ({lead['persona_match']})",
             f"{account['name']} ({account['domain']}) · {account['industry'] or '?'} · {account['size_band'] or '?'} · "
             f"HQ {account['hq_country'] or '?'} · lead country {lead['country'] or '?'}",
             f"email {lead['email']} · {lead['email_class']} via {lead['email_source']} · mailbox {seq['mailbox_id']}",
             *([f"company phone {account['phone']} (their site: {account['phone_evidence']}) · manual follow-up only"]
               if account["phone"] else []),
             f"score {seq['score']} {'(HOLDOUT)' if seq['holdout'] else ''} · reference {seq['reference_policy']}"]
    if score:
        lines.append(f"dims {score['dims_json']}")
        for f in json.loads(score["reviewer_findings_json"] or "[]")[:8]:
            lines.append(f"  reviewer {f.get('from')}: {f.get('verdict')} {f.get('penalty')} {f.get('reason', '')[:120]}")
    lines.append("observations:")
    for o in obs.latest(conn, account["domain"])[:12]:
        lines.append(f"  {o.signal_code:<20} {(o.value_text or o.value_num or '')!s:<40.40} {o.age_days:4.0f}d  {o.evidence_url}")
    for p in conn.execute("SELECT source, url, text_excerpt FROM posts WHERE lead_key = ? OR account_domain = ? LIMIT 2",
                          (seq["lead_key"], account["domain"])):
        lines.append(f"post [{p['source']}] {p['url']}\n  \"{p['text_excerpt'][:300]}\"")
    g = gates.check_lead(cfg=cfg, conn=conn, ledger_conn=ledger_conn, lead=lead, account=account,
                         observations=obs.latest(conn, account["domain"]), score_total=seq["score"])
    lines.append("gates: " + ("all pass" if g.ok else "FAIL " + "; ".join(g.failures)))
    for v in json.loads(seq["validator_json"] or "[]"):
        lines.append(f"VALIDATOR step {v.get('step')}: {v.get('rule')}: {v.get('message')}")
    lines.append("")
    for r in sequence.rendered(sequence.steps_from_json(seq["steps_json"]), vctx):
        lines.append(f"--- email {r['step']}" + (f" · subject: {r['subject']}" if r["subject"] else " · (same thread)"))
        lines.append(r["text"])
    return "\n".join(lines)


def edit_in_editor(steps: list[validate.Step]) -> list[validate.Step]:
    editor = os.environ.get("EDITOR") or "nano"
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as f:
        f.write(to_text(steps))
        path = f.name
    try:
        subprocess.call([editor, path])
        with open(path) as f:
            return from_text(f.read())
    finally:
        os.unlink(path)


def desk(cfg: ProductConfig, conn, ledger_conn, slug: str, private_key, *, batch_min_score: float | None = None) -> dict:
    """The interactive loop. Returns counts."""
    stats = {"approved": 0, "rejected": 0, "edited": 0}
    pending = lambda: conn.execute("SELECT * FROM sequences WHERE status = 'pending_review' "  # noqa: E731
                                   "ORDER BY score DESC").fetchall()
    if batch_min_score is not None:
        rows = [s for s in pending() if (s["score"] or 0) >= batch_min_score and not json.loads(s["validator_json"] or "[]")]
        print(f"{len(rows)} sequence(s) with score >= {batch_min_score}:")
        for s in rows:
            print(f"  {s['score']:5.1f}  {sequence.steps_from_json(s['steps_json'])[0].subject}  -> {s['recipient_email']}")
        typed = input(f"Type {len(rows)} to approve all of these, anything else to cancel: ").strip()
        if rows and typed == str(len(rows)):
            for s in rows:
                try:
                    approve(cfg, conn, slug, s["seq_id"], private_key)
                    stats["approved"] += 1
                except ReviewError as e:
                    print(f"  skipped {s['seq_id']}: {e}")
        return stats
    i = 0
    while True:
        rows = pending()
        if not rows:
            print("Nothing waiting for review.")
            break
        i = max(0, min(i, len(rows) - 1))
        seq = rows[i]
        print("\n" + "=" * 78 + f"\n[{i + 1}/{len(rows)}] sequence {seq['seq_id']}\n" + "=" * 78)
        print(evidence(cfg, conn, ledger_conn, seq))
        key = input("\n[a]pprove  [e]dit  [r]eject  [j] next  [k] previous  [q]uit > ").strip().lower()[:1]
        if key == "q":
            break
        if key == "j":
            i += 1
        elif key == "k":
            i -= 1
        elif key == "a":
            try:
                approve(cfg, conn, slug, seq["seq_id"], private_key)
                stats["approved"] += 1
                print("approved and signed")
            except ReviewError as e:
                print(f"not approved: {e}")
        elif key == "e":
            new = edit_in_editor(sequence.steps_from_json(seq["steps_json"]))
            try:
                v = apply_edit(cfg, conn, seq["seq_id"], new)
                stats["edited"] += 1
                print("saved" + ("" if not v else " with problems: " + "; ".join(f"{x.rule}" for x in v)))
            except ReviewError as e:
                print(e)
        elif key == "r":
            note = input("Why? (feeds the writer; Enter to skip) > ")
            scope = "campaign" if input("Applies to all emails [c] or just this contact [Enter]? ").strip().lower() == "c" else "contact"
            reject(conn, seq["seq_id"], note, scope)
            stats["rejected"] += 1
    return stats


# ---------------------------------------------------------------- reply drafts

def approve_draft(conn, slug: str, draft_id: str, private_key, *, approved_by: str | None = None) -> None:
    from gtm.feedback import draft_hash
    d = conn.execute("SELECT * FROM reply_drafts WHERE id = ?", (draft_id,)).fetchone()
    if not d or d["status"] != "pending_review":
        raise ReviewError("only a pending draft can be approved")
    h = draft_hash(d["id"], d["reply_id"], d["recipient_email"], d["mailbox_id"], d["body"])
    if h != d["content_hash"]:
        raise ReviewError("draft changed since it was written")
    who, at = approved_by or getpass.getuser(), now_iso()
    sig = signing.sign(private_key, signing.approval_payload(product=slug, target="reply_draft", target_id=draft_id,
                                                             content_hash=h, approved_by=who, approved_at=at,
                                                             via="tty_pin", edited=False))
    conn.execute("INSERT INTO approvals (target, target_id, content_hash, approved_by, approved_at, via, edited, signature) "
                 "VALUES ('reply_draft', ?, ?, ?, ?, 'tty_pin', 0, ?)", (draft_id, h, who, at, sig))
    conn.execute("UPDATE reply_drafts SET status = 'approved', updated_at = ? WHERE id = ?", (at, draft_id))


def drafts_desk(conn, slug: str, private_key) -> dict:
    """Snov has no send-reply API (docs/tools.md): approve, send it yourself, mark it sent."""
    stats = {"approved": 0, "sent_manual": 0, "rejected": 0}
    for d in conn.execute("SELECT d.*, r.body_stripped, r.intent FROM reply_drafts d JOIN replies r ON r.id = d.reply_id "
                          "WHERE d.status IN ('pending_review','approved') ORDER BY d.created_at").fetchall():
        print("\n" + "=" * 78 + f"\nreply from {d['recipient_email']} · intent {d['intent']}\n" + "-" * 78)
        print(d["body_stripped"][:1500])
        print("-" * 78 + "\nDRAFT:\n" + d["body"])
        if d["status"] == "pending_review":
            key = input("\n[a]pprove  [r]eject  [Enter] skip > ").strip().lower()[:1]
            if key == "r":
                conn.execute("UPDATE reply_drafts SET status = 'rejected', updated_at = ? WHERE id = ?", (now_iso(), d["id"]))
                stats["rejected"] += 1
                continue
            if key != "a":
                continue
            approve_draft(conn, slug, d["id"], private_key)
            stats["approved"] += 1
        print(f"Send it yourself from mailbox {d['mailbox_id']} as a reply in that thread.")
        if input("Sent it? [y/N] > ").strip().lower() == "y":
            conn.execute("UPDATE reply_drafts SET status = 'sent_manual', updated_at = ? WHERE id = ?", (now_iso(), d["id"]))
            stats["sent_manual"] += 1
    return stats
