"""Small shared helpers: time, hashing, domains, PII patterns."""

from __future__ import annotations

import hashlib
import json
import re
import secrets as _secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 9+ digits with optional separators: phone-like. Years and prices are shorter.
PHONE_RE = re.compile(r"(?:\+?\d[\s().-]?){9,}\d")


def now() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def days_ago(value: str) -> float:
    return (now() - parse_iso(value)) / timedelta(days=1)


def sha1(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()


def sha256(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def new_run_id() -> str:
    return now().strftime("%Y%m%d-%H%M%S-") + _secrets.token_hex(2)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def domain_of(url_or_domain: str) -> str:
    """Bare registrable-ish host: 'https://www.Acme.com/x' -> 'acme.com'."""
    s = (url_or_domain or "").strip().lower()
    if not s:
        return ""
    host = urlparse(s if "://" in s else f"http://{s}").hostname or ""
    return host[4:] if host.startswith("www.") else host


def has_pii(text: str) -> bool:
    return bool(EMAIL_RE.search(text) or PHONE_RE.search(text))


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def sanitize_name(text: str | None, limit: int = 60) -> str:
    """For reports: letters, digits and basic punctuation only (plan §5 Phase 8)."""
    cleaned = re.sub(r"[^\w .,&'()/-]", "", text or "", flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip()[:limit]
