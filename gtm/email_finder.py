"""Pattern-first email finding (plan §5 Phase 5; adapted from Harvey's email_finder.py, MIT).

Learn the company's pattern from a verified address, from addresses printed on their site matched
to named people, or from the shape of their known addresses; build ONE candidate; verify it with
Snov. Harvey cached its 0.3-confidence default pattern forever: here every pattern has a TTL and a
higher-confidence source always replaces a lower one.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import timedelta

from gtm.util import now, now_iso, parse_iso

PATTERNS = ("first.last", "first", "flast", "firstlast", "f.last", "first_last", "firstl", "first.l",
            "last", "last.first", "lastf", "first-last")
SOURCES = {  # source -> (confidence, ttl days)
    "verified": (0.95, 180),
    "site_printed": (0.85, 120),
    "domain_shape": (0.55, 60),
    "default": (0.3, 14),
}
MIN_CANDIDATE_CONFIDENCE = 0.5


def _ascii(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


def split_name(full_name: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", (full_name or "").strip()) if p and not p.endswith(".")]
    parts = [p for p in parts if p.lower() not in {"dr", "mr", "mrs", "ms", "jr", "sr", "phd", "mba"}]
    if not parts:
        return "", ""
    return _ascii(parts[0]), _ascii(parts[-1]) if len(parts) > 1 else ""


def local_part(pattern: str, first: str, last: str) -> str | None:
    if not first or (not last and pattern not in ("first",)):
        return None
    f, l = first, last
    return {
        "first.last": f"{f}.{l}", "first": f, "flast": f"{f[0]}{l}", "firstlast": f"{f}{l}",
        "f.last": f"{f[0]}.{l}", "first_last": f"{f}_{l}", "firstl": f"{f}{l[:1]}", "first.l": f"{f}.{l[:1]}",
        "last": l, "last.first": f"{l}.{f}", "lastf": f"{l}{f[0]}", "first-last": f"{f}-{l}",
    }.get(pattern)


def infer(email: str, full_name: str) -> str | None:
    first, last = split_name(full_name)
    local = email.split("@", 1)[0].lower()
    for p in PATTERNS:
        if local_part(p, first, last) == local:
            return p
    return None


def shape_guess(emails: list[str], generic_prefixes: tuple[str, ...]) -> str | None:
    """No names: guess from address shapes. Many 'x.y@' -> first.last."""
    locals_ = [e.split("@", 1)[0].lower() for e in emails if e.split("@", 1)[0].lower() not in generic_prefixes]
    if len(locals_) < 2:
        return None
    dotted = sum(bool(re.fullmatch(r"[a-z]{2,}\.[a-z]{2,}", x)) for x in locals_)
    if dotted / len(locals_) >= 0.6:
        return "first.last"
    single = sum(bool(re.fullmatch(r"[a-z]{3,}", x)) for x in locals_)
    if single / len(locals_) >= 0.8:
        return "first"
    return None


def get(conn: sqlite3.Connection, domain: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM email_patterns WHERE domain = ?", (domain,)).fetchone()
    if row and parse_iso(row["expires_at"]) < now():
        return None
    return row


def learn(conn: sqlite3.Connection, domain: str, pattern: str, source: str) -> bool:
    """Store unless a still-valid pattern with HIGHER confidence exists. Returns True if stored."""
    conf, ttl = SOURCES[source]
    current = get(conn, domain)
    if current and current["confidence"] > conf:
        return False
    conn.execute("INSERT INTO email_patterns (domain, pattern, confidence, source, learned_at, expires_at) "
                 "VALUES (?,?,?,?,?,?) ON CONFLICT(domain) DO UPDATE SET pattern=excluded.pattern, "
                 "confidence=excluded.confidence, source=excluded.source, learned_at=excluded.learned_at, "
                 "expires_at=excluded.expires_at",
                 (domain, pattern, conf, source, now_iso(), (now() + timedelta(days=ttl)).isoformat(timespec="seconds")))
    return True


def learn_from_site(conn: sqlite3.Connection, domain: str, printed: list[str], people: list[str],
                    generic_prefixes: tuple[str, ...]) -> str | None:
    for email in printed:
        for name in people:
            p = infer(email, name)
            if p:
                learn(conn, domain, p, "site_printed")
                return p
    p = shape_guess(printed, generic_prefixes)
    if p:
        learn(conn, domain, p, "domain_shape")
    return p


def candidate(conn: sqlite3.Connection, domain: str, full_name: str) -> tuple[str | None, float]:
    row = get(conn, domain)
    if not row or row["confidence"] < MIN_CANDIDATE_CONFIDENCE:
        return None, 0.0
    first, last = split_name(full_name)
    local = local_part(row["pattern"], first, last)
    return (f"{local}@{domain}" if local else None), row["confidence"]
