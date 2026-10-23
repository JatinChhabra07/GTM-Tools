"""Sending mailboxes in the global ledger (plan §2.3, §6.3). Limits are PIN-signed: a row whose
signature doesn't verify sends nothing, so nobody can quietly raise a cap."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from gtm import signing, warmup
from gtm.util import now, now_iso

BOUNCE_BRAKE = 0.03
BOUNCE_WINDOW_DAYS = 7


def payload(row: sqlite3.Row | dict) -> dict:
    return signing.mailbox_payload(
        mailbox_id=row["id"], address=row["address"], daily_cap=row["daily_cap"],
        warmup_start=row["warmup_start"], warmup_min_days=row["warmup_min_days"],
        start_sends=row["start_sends"], daily_increase=row["daily_increase"],
        catchall_allowed=bool(row["catchall_allowed"]), brakes=row["brake_count"] if "brake_count" in row.keys() else 0)


def signed_ok(row: sqlite3.Row) -> bool:
    return signing.verify(payload(row), row["signature"])


def add(ledger_conn: sqlite3.Connection, private_key, *, mailbox_id: str, address: str, daily_cap: int,
        warmup_start: str | None, warmup_min_days: int = 14, start_sends: int = 5,
        daily_increase: int = 3, catchall_allowed: bool = False) -> None:
    prev = get(ledger_conn, mailbox_id)
    row = {"id": mailbox_id, "address": address.lower(), "daily_cap": daily_cap,
           "warmup_start": warmup_start, "warmup_min_days": warmup_min_days, "start_sends": start_sends,
           "daily_increase": daily_increase, "catchall_allowed": int(catchall_allowed),
           "brake_count": prev["brake_count"] if prev else 0}
    sig = signing.sign(private_key, payload(row))  # re-signing acknowledges every brake so far
    ledger_conn.execute(
        "INSERT INTO mailboxes (id, address, daily_cap, warmup_start, warmup_min_days, start_sends, "
        "daily_increase, catchall_allowed, signature) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET address=excluded.address, daily_cap=excluded.daily_cap, "
        "warmup_start=excluded.warmup_start, warmup_min_days=excluded.warmup_min_days, "
        "start_sends=excluded.start_sends, daily_increase=excluded.daily_increase, "
        "catchall_allowed=excluded.catchall_allowed, signature=excluded.signature, paused=0",
        (mailbox_id, address.lower(), daily_cap, warmup_start, warmup_min_days, start_sends,
         daily_increase, int(catchall_allowed), sig))


def get(ledger_conn: sqlite3.Connection, mailbox_id: str) -> sqlite3.Row | None:
    return ledger_conn.execute("SELECT * FROM mailboxes WHERE id = ?", (mailbox_id,)).fetchone()


def cap_on(row: sqlite3.Row, day: date) -> int:
    if not signed_ok(row):
        return 0
    return warmup.effective_daily_cap(
        warmup_start=row["warmup_start"], warmup_min_days=row["warmup_min_days"],
        start_sends=row["start_sends"], daily_increase=row["daily_increase"], daily_cap=row["daily_cap"],
        ready_state=row["ready_state"], paused=bool(row["paused"]), on_date=day)


def touches_on(ledger_conn: sqlite3.Connection, mailbox_id: str, day: date) -> int:
    row = ledger_conn.execute("SELECT touches FROM mailbox_sends WHERE mailbox_id = ? AND date = ?",
                              (mailbox_id, day.isoformat())).fetchone()
    return row[0] if row else 0


def touch_days(followup_days: list[int], start: date | None = None) -> list[date]:
    start = start or now().date()
    return [start] + [start + timedelta(days=d) for d in followup_days]


def reserve(ledger_conn: sqlite3.Connection, *, mailbox_id: str, seq_id: str, days: list[date]) -> tuple[bool, str]:
    """Reserve one touch per scheduled day. MUST run inside the caller's BEGIN IMMEDIATE."""
    row = get(ledger_conn, mailbox_id)
    if row is None:
        return False, f"mailbox {mailbox_id} not registered"
    for d in days:
        cap = cap_on(row, d)
        if touches_on(ledger_conn, mailbox_id, d) + 1 > cap:
            return False, f"mailbox {mailbox_id} full on {d.isoformat()} (cap {cap})"
    for d in days:
        ledger_conn.execute(
            "INSERT INTO mailbox_sends (mailbox_id, date, touches) VALUES (?,?,1) "
            "ON CONFLICT(mailbox_id, date) DO UPDATE SET touches = touches + 1", (mailbox_id, d.isoformat()))
        ledger_conn.execute("INSERT OR REPLACE INTO reservations (seq_id, mailbox_id, date, touches) "
                            "VALUES (?,?,?,1)", (seq_id, mailbox_id, d.isoformat()))
    return True, "ok"


def release(ledger_conn: sqlite3.Connection, seq_id: str) -> None:
    """Compensating transaction for a push that failed BEFORE anything was sent."""
    ledger_conn.execute("BEGIN IMMEDIATE")
    try:
        for r in ledger_conn.execute("SELECT * FROM reservations WHERE seq_id = ?", (seq_id,)).fetchall():
            ledger_conn.execute("UPDATE mailbox_sends SET touches = max(0, touches - ?) "
                                "WHERE mailbox_id = ? AND date = ?", (r["touches"], r["mailbox_id"], r["date"]))
        ledger_conn.execute("DELETE FROM reservations WHERE seq_id = ?", (seq_id,))
        ledger_conn.execute("DELETE FROM account_sequences WHERE id = ? AND step_sent = 0", (seq_id,))
        ledger_conn.execute("COMMIT")
    except Exception:
        ledger_conn.execute("ROLLBACK")
        raise


def bounce_rate(ledger_conn: sqlite3.Connection, mailbox_id: str) -> tuple[float, int, int]:
    since = (now() - timedelta(days=BOUNCE_WINDOW_DAYS)).isoformat(timespec="seconds")
    sent = ledger_conn.execute("SELECT count(*) FROM contact_log WHERE mailbox_id = ? AND sent_at >= ?",
                               (mailbox_id, since)).fetchone()[0]
    bounced = ledger_conn.execute("SELECT count(*) FROM bounces WHERE mailbox_id = ? AND at >= ?",
                                  (mailbox_id, since)).fetchone()[0]
    return (bounced / sent if sent else (1.0 if bounced else 0.0)), bounced, sent


def apply_bounce_brake(ledger_conn: sqlite3.Connection, mailbox_id: str) -> bool:
    """Rolling 7-day bounce rate above 3% pauses the mailbox. Returns True if it paused it now."""
    rate, bounced, _ = bounce_rate(ledger_conn, mailbox_id)
    ledger_conn.execute("UPDATE mailboxes SET bounce_rate_7d = ? WHERE id = ?", (rate, mailbox_id))
    if bounced and rate > BOUNCE_BRAKE:
        cur = ledger_conn.execute("UPDATE mailboxes SET paused = 1, brake_count = brake_count + 1 "
                                  "WHERE id = ? AND paused = 0", (mailbox_id,))
        return cur.rowcount == 1
    return False


def set_ready(ledger_conn: sqlite3.Connection, domain: str, verdict: str) -> int:
    state = "ready" if verdict == "READY" else "not_ready"
    cur = ledger_conn.execute("UPDATE mailboxes SET ready_state = ?, ready_checked_at = ? "
                              "WHERE lower(address) LIKE ?", (state, now_iso(), f"%@{domain.lower()}"))
    return cur.rowcount
