"""Sending-domain readiness and the per-mailbox cold-email ramp (plan §5 Phase 0, §6.3).

Adapted from Harvey's `deliverability.py` and `config.py` warmup ramp (MIT, credited in NOTICE).
Differences from Harvey:
- NOT READY **blocks** sending here; Harvey only advised.
- SPF lookups are counted recursively through `include:`/`redirect=`, not just the top level.
- A DKIM lookup failure on one selector doesn't stop the search on the others.

Everything is read-only and free: public DNS, RDAP and DNS blocklists. DNS and RDAP are injected
(`resolver`, `rdap_fetch`) so tests never touch the network.

A failed LOOKUP (timeout, SERVFAIL) is "unknown", never "fail": we can't tell the founder his
domain is broken when it's our connection that is.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

from gtm.util import domain_of, now as _now, parse_iso

PASS, WARN, FAIL, UNKNOWN = "pass", "warn", "fail", "unknown"
READY, NOT_READY = "READY", "NOT READY"

DEFAULT_DKIM_SELECTORS: tuple[str, ...] = (
    "google", "selector1", "selector2", "default", "k1", "s1", "s2", "mail", "dkim", "snov",
    "zoho", "mailjet", "sendgrid", "smtp", "everlytic",
)

# (zone, "the list refused to answer" predicate). Public resolvers such as 8.8.8.8 are blocked by
# these lists and get a sentinel answer instead of a verdict.
DOMAIN_BLOCKLISTS: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("dbl.spamhaus.org", lambda ip: ip.startswith("127.255.255.")),
    ("multi.surbl.org", lambda ip: ip == "127.0.0.1"),
)
MANUAL_LOOKUP = {
    "dbl.spamhaus.org": "https://check.spamhaus.org",
    "multi.surbl.org": "https://www.surbl.org/surbl-analysis",
}

SPF_LOOKUP_LIMIT = 10  # RFC 7208 §4.6.4
SPF_MAX_DEPTH = 10
SPF_LOOKUP_MECHANISMS = ("include", "a", "mx", "ptr", "exists", "redirect")
MIN_DOMAIN_AGE_DAYS = 30
GOOD_DOMAIN_AGE_DAYS = 90
# Without these three we can't say the domain is safe, so "unknown" blocks too.
CORE_CHECKS = ("mx", "spf", "dmarc")

DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")

Resolver = Callable[[str, str], list[str]]


@dataclass
class Check:
    name: str
    status: str
    detail: str


@dataclass
class Report:
    domain: str
    checks: list[Check]
    verdict: str
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- default network adapters

def dns_resolver(name: str, rdtype: str) -> list[str]:
    """dnspython with a 5 s lifetime. NXDOMAIN/no answer -> LookupError; other failures propagate."""
    import dns.resolver

    try:
        answer = dns.resolver.resolve(name, rdtype, lifetime=5)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer) as e:
        raise LookupError(f"{name} {rdtype}: no record") from e
    out = []
    for rdata in answer:
        if rdtype == "TXT":
            out.append(b"".join(rdata.strings).decode("utf-8", "replace"))
        elif rdtype == "A":
            out.append(rdata.address)
        else:
            out.append(rdata.to_text())
    return out


def rdap_lookup(domain: str) -> dict:
    import httpx

    r = httpx.get(f"https://rdap.org/domain/{domain}", timeout=10, follow_redirects=True)
    r.raise_for_status()
    return r.json()


def _lookup(resolver: Resolver, name: str, rdtype: str) -> list[str] | None:
    """Records, [] for NXDOMAIN/no answer, None when the lookup itself failed."""
    try:
        return list(resolver(name, rdtype))
    except LookupError:
        return []
    except Exception:
        return None


def _unknown(name: str, what: str) -> Check:
    return Check(name, UNKNOWN, f"We couldn't look up your {what} (network or DNS problem on our "
                 "side, not necessarily yours). Check your connection and run it again.")


# ---------------------------------------------------------------- checks

def check_mx(domain: str, resolver: Resolver) -> Check:
    records = _lookup(resolver, domain, "MX")
    if records is None:
        return _unknown("mx", "mail servers (MX)")
    hosts = [r.split()[-1].rstrip(".") for r in records if r.split()]
    hosts = [h for h in hosts if h]  # "0 ." is a null MX: the domain says it takes no mail
    if not hosts:
        return Check("mx", FAIL, "Your domain has no mail server (MX record), so replies and "
                     "bounces have nowhere to go. Add the MX records your email provider gives you.")
    return Check("mx", PASS, f"Mail servers found: {', '.join(hosts[:3])}.")


def _spf_records(txts: list[str]) -> list[str]:
    return [t.strip() for t in txts if t.strip().lower().split(" ")[0] == "v=spf1"]


def _mechanism(term: str) -> str:
    return re.split(r"[:=/]", term.lstrip("+-~?"), maxsplit=1)[0]


def _target(term: str) -> str:
    return re.split(r"[:=]", term, maxsplit=1)[1] if re.search(r"[:=]", term) else ""


def count_spf_lookups(record: str, resolver: Resolver, depth: int = 0,
                      seen: frozenset[str] = frozenset()) -> tuple[int, bool]:
    """(DNS-lookup count including nested includes, whether every nested lookup succeeded)."""
    count, complete = 0, True
    for term in record.lower().split()[1:]:
        mech = _mechanism(term)
        if mech not in SPF_LOOKUP_MECHANISMS:
            continue
        count += 1
        if mech not in ("include", "redirect"):
            continue
        target = _target(term)
        # Past the depth limit (or a loop) the record is broken anyway; >10 lookups already.
        if not target or target in seen or depth + 1 > SPF_MAX_DEPTH:
            continue
        txts = _lookup(resolver, target, "TXT")
        if txts is None:
            complete = False
            continue
        nested = _spf_records(txts)
        if len(nested) == 1:
            sub, ok = count_spf_lookups(nested[0], resolver, depth + 1, seen | {target})
            count, complete = count + sub, complete and ok
        if count > SPF_LOOKUP_LIMIT:
            break
    return count, complete


def check_spf(domain: str, resolver: Resolver) -> Check:
    txts = _lookup(resolver, domain, "TXT")
    if txts is None:
        return _unknown("spf", "SPF record")
    records = _spf_records(txts)
    if not records:
        return Check("spf", FAIL, "Your domain has no SPF record, so receivers can't tell your "
                     "emails are really from you. Add the SPF TXT record your email provider gives you.")
    if len(records) > 1:
        return Check("spf", FAIL, f"Your domain has {len(records)} SPF records. Receivers treat that "
                     "as having none. Merge them into one record.")
    spf = records[0]
    terms = spf.lower().split()
    if "+all" in terms or "all" in terms:
        return Check("spf", FAIL, f"Your SPF record ends in '+all', which lets anyone on the internet "
                     f"send email as you. Change it to '~all'. Current record: {spf}")
    lookups, complete = count_spf_lookups(spf, resolver)
    if lookups > SPF_LOOKUP_LIMIT:
        return Check("spf", FAIL, f"Your SPF record needs {lookups} DNS lookups; the limit is "
                     f"{SPF_LOOKUP_LIMIT}, so receivers will reject it as broken. Remove services you "
                     "no longer send from.")
    if not complete:
        return Check("spf", UNKNOWN, "We couldn't follow every 'include' in your SPF record, so we "
                     "can't confirm it stays under the 10-lookup limit. Run the check again.")
    if "?all" in terms:
        return Check("spf", WARN, f"Your SPF record ends in '?all' (neutral), which protects nothing. "
                     f"Change it to '~all'. Current record: {spf}")
    if not any(_mechanism(t) == "all" or t.startswith("redirect=") for t in terms):
        return Check("spf", WARN, f"Your SPF record has no 'all' at the end, so it ends neutral. "
                     f"Add '~all'. Current record: {spf}")
    return Check("spf", PASS, f"SPF is set up ({lookups} of {SPF_LOOKUP_LIMIT} lookups used).")


def check_dkim(domain: str, resolver: Resolver, selectors: list[str] | None = None) -> Check:
    explicit = bool(selectors)
    any_failed = False
    for sel in selectors or DEFAULT_DKIM_SELECTORS:
        txts = _lookup(resolver, f"{sel}._domainkey.{domain}", "TXT")
        if txts is None:
            any_failed = True
            continue
        for record in txts:
            compact = record.replace(" ", "").lower()
            # "p=" with an empty value means the key was revoked.
            has_key = re.search(r"(^|;)p=[^;]+", compact) is not None
            if has_key and ("v=dkim1" in compact or "k=" in compact or compact.startswith("p=")):
                return Check("dkim", PASS, f"DKIM signing key found (selector '{sel}').")
    if any_failed:
        return _unknown("dkim", "DKIM key")
    if explicit:
        return Check("dkim", FAIL, f"No DKIM key at selector {', '.join(selectors)}. Turn on DKIM "
                     "signing in your email provider and add the record it gives you.")
    return Check("dkim", WARN, "We didn't find a DKIM key under the common names. If your provider "
                 "uses a custom selector, run the check again with it; otherwise turn on DKIM "
                 "signing in your email provider.")


def check_dmarc(domain: str, resolver: Resolver) -> Check:
    txts = _lookup(resolver, f"_dmarc.{domain}", "TXT")
    if txts is None:
        return _unknown("dmarc", "DMARC record")
    records = [t.strip() for t in txts if t.strip().lower().startswith("v=dmarc1")]
    if not records:
        return Check("dmarc", FAIL, "Your domain has no DMARC record. Gmail and Yahoo require one. "
                     f"Add a TXT record at _dmarc.{domain} (start with p=none, then move to quarantine).")
    if len(records) > 1:
        return Check("dmarc", FAIL, f"Your domain has {len(records)} DMARC records, so receivers "
                     "ignore all of them. Keep just one.")
    tags = dict(part.strip().split("=", 1) for part in records[0].split(";") if "=" in part)
    policy = {k.strip().lower(): v.strip().lower() for k, v in tags.items()}.get("p", "")
    if policy in ("quarantine", "reject"):
        return Check("dmarc", PASS, f"DMARC policy is '{policy}'.")
    if policy == "none":
        return Check("dmarc", WARN, "DMARC is in monitoring mode (p=none). Fine while warming up; "
                     "move to p=quarantine once your reports look clean.")
    return Check("dmarc", WARN, "Your DMARC record has no valid policy (p=). Add p=none to start.")


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else parse_iso(value.isoformat())
    if isinstance(value, date):
        return parse_iso(value.isoformat())
    return parse_iso(str(value))


def check_domain_age(domain: str, fetch: Callable[[str], dict], now) -> Check:
    try:
        events = (fetch(domain) or {}).get("events") or []
    except Exception:
        return _unknown("domain_age", "domain registration date")
    registered = next((e.get("eventDate") for e in events
                       if isinstance(e, dict) and e.get("eventAction") == "registration"), None)
    try:
        age = (_as_datetime(now) - parse_iso(registered)).days if registered else None
    except (TypeError, ValueError):
        age = None
    if age is None:
        return Check("domain_age", UNKNOWN, "The domain registry didn't give us a registration "
                     "date, so we can't tell how old the domain is.")
    if age < MIN_DOMAIN_AGE_DAYS:
        return Check("domain_age", FAIL, f"Your domain is only {age} days old. Brand-new domains are "
                     f"distrusted: warm it up and wait until it is at least {MIN_DOMAIN_AGE_DAYS} "
                     "days old before cold email.")
    if age < GOOD_DOMAIN_AGE_DAYS:
        return Check("domain_age", WARN, f"Your domain is {age} days old: still young, so keep "
                     "volume low.")
    return Check("domain_age", PASS, f"Your domain is {age} days old.")


def check_blocklists(domain: str, resolver: Resolver) -> list[Check]:
    checks = []
    for zone, refused in DOMAIN_BLOCKLISTS:
        name = f"blocklist:{zone}"
        hits = _lookup(resolver, f"{domain}.{zone}", "A")
        if hits is None:
            checks.append(_unknown(name, f"listing on {zone}"))
        elif not hits:
            checks.append(Check(name, PASS, f"Not listed on {zone}."))
        elif any(refused(ip) for ip in hits):
            checks.append(Check(name, UNKNOWN, f"{zone} refused to answer our query (it blocks "
                                "public DNS servers). Check by hand at "
                                f"{MANUAL_LOOKUP.get(zone, zone)}."))
        else:
            checks.append(Check(name, FAIL, f"Your domain is LISTED on the {zone} spam blocklist. "
                                "Don't send from it; check the reason and request removal at "
                                f"{MANUAL_LOOKUP.get(zone, zone)}."))
    return checks


# ---------------------------------------------------------------- report

def verdict(checks: list[Check]) -> tuple[str, list[str]]:
    reasons = [c.detail for c in checks if c.status == FAIL]
    reasons += [c.detail for c in checks if c.name in CORE_CHECKS and c.status == UNKNOWN]
    return (NOT_READY if reasons else READY), reasons


def check_domain(domain: str, *, dkim_selectors: list[str] | None = None,
                 resolver: Resolver | None = None, rdap_fetch: Callable[[str], dict] | None = None,
                 now=None) -> Report:
    """Run every readiness check on a sending domain. Raises ValueError for a malformed domain."""
    d = domain_of(domain)
    if not DOMAIN_RE.match(d):
        raise ValueError(f"'{domain}' doesn't look like a domain (expected something like acme.com)")
    resolver = resolver or dns_resolver
    fetch = rdap_fetch or rdap_lookup
    checks = [
        check_mx(d, resolver),
        check_spf(d, resolver),
        check_dkim(d, resolver, dkim_selectors),
        check_dmarc(d, resolver),
        check_domain_age(d, fetch, now if now is not None else _now()),
        *check_blocklists(d, resolver),
    ]
    v, reasons = verdict(checks)
    return Report(domain=d, checks=checks, verdict=v, reasons=reasons)


# ---------------------------------------------------------------- ramp

def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    return date.fromisoformat(s) if len(s) == 10 else parse_iso(s).date()


def warmup_day(warmup_start, on_date) -> int:
    """Whole days since warmup started: the start date itself is day 0."""
    return (_as_date(on_date) - _as_date(warmup_start)).days


def effective_daily_cap(*, warmup_start: str | None, warmup_min_days: int, start_sends: int,
                        daily_increase: int, daily_cap: int, ready_state: str, paused: bool,
                        on_date: date) -> int:
    """Cold emails this mailbox may send on `on_date` (plan §6.3, Harvey config.py ramp).

    Day counting: `warmup_start` is day 0, so with warmup_min_days=14 day 13 sends nothing, day 14
    sends `start_sends`, day 15 sends `start_sends + daily_increase`, never above `daily_cap`.
    """
    if paused or ready_state != "ready" or not warmup_start:
        return 0
    days = warmup_day(warmup_start, on_date)
    if days < warmup_min_days:
        return 0
    return max(0, min(daily_cap, start_sends + daily_increase * (days - warmup_min_days)))
