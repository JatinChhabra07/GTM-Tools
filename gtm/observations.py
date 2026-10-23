"""Observations: every fact is a row (plan §0 #5, §3). Writes go through `record`; the database
trigger refuses unknown or unconfirmed codes, so no code path can collect a signal the founder
didn't confirm.

Cohort queries are adapted from Harvey's state.py with its bugs fixed: numeric codes compare against
the code's threshold, `min_confidence` is enforced, and only the LATEST observation per
(account, code) counts.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from gtm.util import days_ago, now_iso


class UnconfirmedSignal(Exception):
    pass


def confirmed_codes(conn: sqlite3.Connection, kind: str | None = None) -> set[str]:
    sql = "SELECT code FROM signal_codes WHERE status = 'confirmed'"
    args: tuple = ()
    if kind:
        sql += " AND kind = ?"
        args = (kind,)
    return {r[0] for r in conn.execute(sql, args)}


def is_confirmed(conn: sqlite3.Connection, code: str) -> bool:
    return conn.execute("SELECT 1 FROM signal_codes WHERE code = ? AND status = 'confirmed'",
                        (code,)).fetchone() is not None


def record(conn: sqlite3.Connection, *, account_domain: str, signal_code: str, collector: str,
           evidence_url: str, confidence: float, value_num: float | None = None,
           value_text: str | None = None, lead_key: str | None = None, run_id: str | None = None,
           observed_at: str | None = None) -> int | None:
    """Insert one observation. Returns its id, or None when the code isn't confirmed (skipped, not
    an error: collectors call this freely and the vocabulary decides)."""
    if not is_confirmed(conn, signal_code):
        return None
    cur = conn.execute(
        "INSERT INTO observations (account_domain, lead_key, collector, signal_code, value_num, "
        "value_text, confidence, evidence_url, observed_at, run_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (account_domain.lower(), lead_key, collector, signal_code, value_num,
         (value_text or None) and value_text[:500], max(0.0, min(1.0, confidence)), evidence_url,
         observed_at or now_iso(), run_id))
    return cur.lastrowid


def record_health(conn: sqlite3.Connection, *, account_domain: str, code: str, collector: str,
                  evidence_url: str, detail: str, run_id: str | None = None) -> int | None:
    """Failures are observations (health codes are system codes: always confirmed)."""
    return record(conn, account_domain=account_domain, signal_code=code, collector=collector,
                  evidence_url=evidence_url, confidence=1.0, value_text=detail[:300], run_id=run_id)


@dataclass
class Latest:
    id: int
    account_domain: str
    signal_code: str
    kind: str
    value_num: float | None
    value_text: str | None
    confidence: float
    evidence_url: str
    observed_at: str
    lead_key: str | None
    rank: int
    freshness_days: int | None

    @property
    def age_days(self) -> float:
        return days_ago(self.observed_at)

    @property
    def fresh(self) -> bool:
        return self.freshness_days is None or self.age_days <= self.freshness_days


# How "latest" works per code:
# - SNAPSHOT codes are re-observed as a whole set by one profiler pass (their tech stack, team page):
#   current = every row from the most recent pass, so a tool that disappeared stops counting.
# - EVENT codes (intent, trigger, health) are distinct happenings: latest row per distinct value.
# - everything else is single-valued: the latest row wins.
SNAPSHOT_CODES = {"TECH_STACK", "TEAM_MEMBER_FOUND", "DECISION_MAKER_TITLE", "GENERIC_INBOX",
                  "COMPETITOR_CUSTOMER"}
EVENT_KINDS = {"intent", "trigger", "health"}

_ROWS_SQL = """
SELECT o.id, o.account_domain, o.signal_code, c.kind, o.value_num, o.value_text, o.confidence,
       o.evidence_url, o.observed_at, o.lead_key, o.run_id, c.rank, c.freshness_days,
       c.min_confidence, c.threshold_op, c.threshold_value, c.value_type
FROM observations o JOIN signal_codes c ON c.code = o.signal_code
WHERE o.account_domain = ? AND c.status = 'confirmed'
ORDER BY o.observed_at DESC, o.id DESC
"""


def _passes(row: sqlite3.Row) -> bool:
    """Present = confident enough AND (bool: truthy; num: meets threshold; text: non-empty)."""
    if row["confidence"] < row["min_confidence"]:
        return False
    if row["value_type"] == "num":
        if row["value_num"] is None:
            return False
        if row["threshold_op"] is None:
            return row["value_num"] != 0
        v, t = row["value_num"], row["threshold_value"]
        return {">": v > t, ">=": v >= t, "<": v < t, "<=": v <= t, "=": v == t, "!=": v != t}[row["threshold_op"]]
    if row["value_type"] == "text":
        return bool(row["value_text"])
    return row["value_num"] is None or row["value_num"] != 0


def latest(conn: sqlite3.Connection, account_domain: str, *, only_present: bool = True,
           kinds: tuple[str, ...] | None = None) -> list[Latest]:
    rows = conn.execute(_ROWS_SQL, (account_domain.lower(),)).fetchall()
    keep: list[sqlite3.Row] = []
    seen: set = set()
    batch: dict[str, tuple] = {}
    for r in rows:  # newest first
        code = r["signal_code"]
        if kinds and r["kind"] not in kinds:
            continue
        if code in SNAPSHOT_CODES:
            marker = (r["run_id"], r["observed_at"][:16]) if r["run_id"] is None else (r["run_id"],)
            batch.setdefault(code, marker)
            if batch[code] != marker or (code, r["value_text"]) in seen:
                continue
            seen.add((code, r["value_text"]))
        elif r["kind"] in EVENT_KINDS:
            if (code, r["value_text"]) in seen:
                continue
            seen.add((code, r["value_text"]))
        else:
            if code in seen:
                continue
            seen.add(code)
        keep.append(r)
    keep.sort(key=lambda r: (r["rank"], r["observed_at"]))
    return [Latest(**{k: r[k] for k in Latest.__dataclass_fields__})
            for r in keep if not only_present or _passes(r)]


def cohort(conn: sqlite3.Connection, *, must: list[str] = (), must_not: list[str] = (),
           domains: list[str] | None = None) -> list[str]:
    """Accounts whose latest, confident, threshold-passing observations include every `must` code
    and none of the `must_not` codes."""
    if domains is None:
        domains = [r[0] for r in conn.execute("SELECT DISTINCT account_domain FROM observations")]
    out = []
    for d in domains:
        present = {o.signal_code for o in latest(conn, d)}
        if all(c in present for c in must) and not any(c in present for c in must_not):
            out.append(d)
    return sorted(out)


def for_ids(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    if not ids:
        return []
    return conn.execute(f"SELECT * FROM observations WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
