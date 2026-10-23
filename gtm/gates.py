"""Hard gates (plan §4.1). Deterministic: any failure drops the lead and no LLM can override it.

Each gate returns (ok, reason). `check_lead` runs them all and is called at qualify time; the outbox
re-runs `check_lead` + the ledger navigation check after its atomic claim (plan §5 Phase 7), so a
lead that went stale or got suppressed between approval and push is never sent.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta

from gtm import ledger
from gtm.config import ProductConfig
from gtm.observations import Latest
from gtm.util import domain_of, now, parse_iso

EU = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV",
      "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}
EEA_EXTRA = {"IS", "LI", "NO"}
GROUPS = {"EU": EU | EEA_EXTRA, "UK": {"GB"}}
# ccTLD -> country, only used to ADD a block (plan §4.1); generic TLDs are neutral
CCTLD = {"in": "IN", "uk": "GB", "de": "DE", "fr": "FR", "es": "ES", "it": "IT", "nl": "NL", "se": "SE",
         "ie": "IE", "pl": "PL", "pt": "PT", "be": "BE", "at": "AT", "dk": "DK", "fi": "FI", "eu": "DE",
         "ch": "CH", "no": "NO", "au": "AU", "ca": "CA", "sg": "SG", "ae": "AE", "jp": "JP", "br": "BR"}
COUNTRY_NAMES = {"india": "IN", "united states": "US", "usa": "US", "united states of america": "US",
                 "united kingdom": "GB", "uk": "GB", "england": "GB", "germany": "DE", "france": "FR",
                 "canada": "CA", "australia": "AU", "singapore": "SG", "united arab emirates": "AE",
                 "netherlands": "NL", "spain": "ES", "ireland": "IE", "italy": "IT", "sweden": "SE"}
GENERIC_TLDS = {"com", "io", "co", "ai", "net", "org", "app", "dev", "so", "xyz", "tech", "biz", "info"}


def country_code(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if len(v) == 2 and v.isalpha():
        return "GB" if v.upper() == "UK" else v.upper()
    low = v.lower()
    if low in COUNTRY_NAMES:
        return COUNTRY_NAMES[low]
    tail = low.rsplit(",", 1)[-1].strip()  # "Bengaluru, Karnataka, India"
    return COUNTRY_NAMES.get(tail)


def geo_rule(cfg: ProductConfig, country: str | None):
    if country:
        if country in cfg.geo_policy:
            return cfg.geo_policy[country]
        for group, members in GROUPS.items():
            if country in members and group in cfg.geo_policy:
                return cfg.geo_policy[group]
    return cfg.geo_policy["default"]


def geo_allowed(cfg: ProductConfig, *, lead_country: str | None, hq_country: str | None,
                domain: str) -> tuple[bool, str, str | None]:
    """(ok, reason, notice). Location decides; a ccTLD can only add a block; unknown = blocked."""
    country = country_code(lead_country) or country_code(hq_country)
    tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
    if not country:
        return False, "no known location (unknown = blocked)", None
    rule = geo_rule(cfg, country)
    if rule.status == "blocked":
        return False, f"country {country} is blocked by geo_policy", None
    if country == "IN" and not (cfg.geo_policy.get("IN") and cfg.geo_policy["IN"].legal_signoff):
        return False, "India needs legal_signoff before any send", None
    tld_country = CCTLD.get(tld)
    if tld_country and tld_country != country and geo_rule(cfg, tld_country).status == "blocked":
        return False, f".{tld} domain points to blocked {tld_country}", None
    notice = rule.notice if rule.status == "allowed_with_notice" else None
    return True, f"country {country} {rule.status}", notice


@dataclass
class GateResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    notice: str | None = None
    checks: dict[str, bool] = field(default_factory=dict)

    def add(self, name: str, ok: bool, reason: str = "") -> None:
        self.checks[name] = ok
        if not ok:
            self.ok = False
            self.failures.append(f"{name}: {reason}" if reason else name)


def check_lead(*, cfg: ProductConfig, conn: sqlite3.Connection, ledger_conn: sqlite3.Connection,
               lead: sqlite3.Row, account: sqlite3.Row, observations: list[Latest],
               score_total: float | None = None, catchall_share_ok: bool = True,
               mailbox_catchall_allowed: bool = False) -> GateResult:
    res = GateResult(ok=True)
    domain = account["domain"]
    email = lead["email"]

    # email class
    cls = lead["email_class"]
    if not email:
        res.add("email", False, "no email")
    elif cls == "valid":
        res.add("email", True)
    elif cls == "catchall":
        ok = (cfg.scoring.allow_catchall and score_total is not None and score_total >= 85
              and catchall_share_ok and mailbox_catchall_allowed)
        res.add("email", ok, "catch-all address not allowed for this lead")
    else:
        res.add("email", False, f"email is {cls or 'unverified'}")
    if lead["is_generic_inbox"]:
        printed = any(o.signal_code == "GENERIC_INBOX" and (o.value_text or "").lower() == (email or "")
                      for o in observations)
        named = conn.execute("SELECT 1 FROM leads WHERE account_domain = ? AND is_generic_inbox = 0 "
                             "AND email IS NOT NULL AND email_class = 'valid' LIMIT 1", (domain,)).fetchone()
        res.add("generic_inbox", printed and not named and cls == "valid",
                "generic inbox only if printed on their site, verified, and no named person found")

    # not a client / competitor / suppressed
    is_client = conn.execute("SELECT 1 FROM clients WHERE domain = ?", (domain,)).fetchone() is not None
    res.add("not_client", not is_client, "existing client")
    competitor_names = {c.lower() for c in cfg.signals.competitors}
    competitor_domains = {domain_of(c) for c in cfg.signals.competitors if "." in c}
    is_comp = domain in competitor_domains or (account["name"] or "").lower() in competitor_names
    res.add("not_competitor", not is_comp, "competitor")
    res.add("not_suppressed", not ledger.is_suppressed(ledger_conn, email=email, domain=domain,
                                                         linkedin_url=lead["linkedin_url"]), "suppressed")
    anti = [a.lower() for a in cfg.anti_icp]
    industry = (account["industry"] or "").lower()
    res.add("not_anti_icp", not any(a and a in industry for a in anti), "anti-ICP industry")

    # freshness: at least one fresh intent/trigger observation behind the lead
    signals = [o for o in observations if o.kind in ("intent", "trigger")]
    res.add("fresh_signal", any(o.fresh for o in signals), "no fresh signal or trigger")

    # persona + seniority
    res.add("persona", bool(lead["persona"]) and bool(lead["persona_match"]), "title matches no persona")

    ok, reason, notice = geo_allowed(cfg, lead_country=lead["country"], hq_country=account["hq_country"],
                                     domain=domain)
    res.add("geo", ok, reason)
    res.notice = notice
    return res


def navigation_ok(ledger_conn: sqlite3.Connection, *, product: str, domain: str, lead_key: str,
                  referral: bool, recontact_days: int = 90) -> tuple[bool, str]:
    """Account navigation across ALL products (plan §4.1). Call inside BEGIN IMMEDIATE at claim time."""
    cutoff = (now() - timedelta(days=recontact_days)).isoformat(timespec="seconds")
    rows = ledger_conn.execute(
        "SELECT product, lead_key, status, started_at, ended_at FROM account_sequences WHERE domain = ?",
        (domain,)).fetchall()
    for r in rows:
        if r["status"] == "active":
            return False, f"account already has an active sequence ({r['product']})"
    for r in rows:
        if r["product"] != product and (r["ended_at"] or r["started_at"]) >= cutoff:
            return False, f"contacted by {r['product']} within {recontact_days} days"
        if r["status"] in ("replied", "opted_out", "bounced") and (r["ended_at"] or "") >= cutoff:
            if not (referral and r["status"] == "replied"):
                return False, f"account closed for {recontact_days} days after a {r['status']} sequence"
    mine = [r for r in rows if r["product"] == product and r["started_at"] >= cutoff]
    silent = [r for r in mine if r["status"] == "finished"]
    if len(silent) >= 3:
        return False, "3 silent contacts at this account in 90 days"
    last_end = max((r["ended_at"] or "" for r in mine), default="")
    if last_end:
        if _business_days_between(parse_iso(last_end), now()) < 5:
            return False, "next contact only 5 business days after the previous sequence finished"
    return True, "ok"


def _business_days_between(a, b) -> int:
    days, d = 0, a.date()
    while d < b.date():
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days
