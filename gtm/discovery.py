"""Engine attribution (plan §15, D4): which discovery engine found each company.

A company can be found by several engines, so a hit is one row per (domain, engine); a repeat
find bumps `hits` and `last_seen_at`. Rows hold company domains, queries and company/list/job URLs
only, never a person (erasure needs nothing here: a named person's post is attributed by the
company's own resolution evidence, not the post URL).

Callers:
  icp        accounts.candidates_from_search (ICP queries, and lists those queries surfaced)
  lookalike  accounts.candidates_from_search ("companies like <client>", "<client> competitors";
             seeds are clients, then the founder's dream customers)
  social     resolve.py (a post's employer resolved) and triggers.py (a job post)
  manual     `accounts import` (the founder's own list) and dream_customers.csv
  competitor HOOK for D2: wherever a competitor's client becomes an account, call
             `discovery.record(conn, domain, "competitor", evidence_url=<logo wall / case study /
             review URL>, detail="<competitor>: <logo wall|case study|review|uses-x post>")`
             right after `accounts.ensure_account(...)`. Nothing else is needed: the per-engine
             funnel in report.py picks it up.
"""

from __future__ import annotations

import sqlite3

from gtm.util import domain_of, now_iso

ENGINES = ("icp", "lookalike", "social", "competitor", "manual")
BACKFILL = {"signal": "social", "tal": "icp", "competitor": "competitor"}


def record(conn: sqlite3.Connection, domain: str, engine: str, *, evidence_url: str, detail: str | None = None) -> bool:
    """Upsert one hit. False (nothing written) without a domain or an http(s) evidence URL."""
    if engine not in ENGINES:
        raise ValueError(f"unknown discovery engine {engine!r}")
    domain = domain_of(domain)
    if not domain or not (evidence_url or "").startswith(("http://", "https://")):
        return False
    at = now_iso()
    conn.execute(
        "INSERT INTO discovery_hits (domain, engine, detail, evidence_url, first_seen_at, last_seen_at, hits) "
        "VALUES (?,?,?,?,?,?,1) ON CONFLICT(domain, engine) DO UPDATE SET hits = hits + 1, "
        "last_seen_at = excluded.last_seen_at, "
        # a backfilled row learns the real query the first time the engine finds the company again
        "detail = CASE WHEN detail = 'backfill' THEN excluded.detail ELSE detail END, "
        "evidence_url = CASE WHEN detail = 'backfill' THEN excluded.evidence_url ELSE evidence_url END",
        (domain, engine, (detail or "")[:300] or None, evidence_url, at, at))
    return True


# an old account with no created_at: counted all-time, never as "found in the last N days"
UNKNOWN_FIRST_SEEN = "2000-01-01T00:00:00+00:00"


def backfill(conn: sqlite3.Connection) -> int:
    """One-time attribution for accounts that predate this table: source 'signal' -> social,
    'tal' -> icp (detail 'backfill'). Clients and near-misses were never discovered. Idempotent."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO discovery_hits (domain, engine, detail, evidence_url, first_seen_at, last_seen_at, hits) "
        "SELECT a.domain, CASE a.source " + " ".join(f"WHEN '{s}' THEN '{e}'" for s, e in BACKFILL.items()) + " END, "
        "'backfill', a.resolution_evidence, COALESCE(a.created_at, ?), COALESCE(a.created_at, ?), 1 FROM accounts a "
        f"WHERE a.source IN ({','.join('?' * len(BACKFILL))}) AND a.status NOT IN ('client','near_miss') "
        "AND (a.resolution_evidence GLOB 'http://*' OR a.resolution_evidence GLOB 'https://*') "
        "AND NOT EXISTS (SELECT 1 FROM discovery_hits h WHERE h.domain = a.domain)",
        (UNKNOWN_FIRST_SEEN, UNKNOWN_FIRST_SEEN, *BACKFILL))
    return cur.rowcount


def engines_for(conn: sqlite3.Connection, domain: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT engine FROM discovery_hits WHERE domain = ? ORDER BY first_seen_at",
                                       (domain_of(domain),))]


def domains_by_engine(conn: sqlite3.Connection, since: str | None = None) -> dict[str, set[str]]:
    """engine -> domains it found (first seen at/after `since`), clients excluded."""
    out: dict[str, set[str]] = {e: set() for e in ENGINES}
    for r in conn.execute("SELECT h.engine, h.domain FROM discovery_hits h WHERE h.first_seen_at >= ? "
                          "AND NOT EXISTS (SELECT 1 FROM clients c WHERE c.domain = h.domain)", (since or "",)):
        out.setdefault(r["engine"], set()).add(r["domain"])
    return out
