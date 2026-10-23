"""Domain readiness checks and the mailbox ramp (plan §5 Phase 0, §6.3). No network: DNS and RDAP
are fakes."""

from datetime import UTC, date, datetime

import pytest

from gtm import warmup
from gtm.warmup import check_domain, effective_daily_cap

D = "acme.example"
NOW = datetime(2026, 10, 23, tzinfo=UTC)


class Timeout(Exception):
    pass


def healthy() -> dict:
    return {
        (D, "MX"): ["10 aspmx.l.google.com."],
        (D, "TXT"): ["google-site-verification=x", "v=spf1 include:_spf.google.com ~all"],
        ("_spf.google.com", "TXT"): ["v=spf1 include:_netblocks.google.com ~all"],
        ("_netblocks.google.com", "TXT"): ["v=spf1 ip4:35.190.247.0/24 ~all"],
        (f"google._domainkey.{D}", "TXT"): ["v=DKIM1; k=rsa; p=MIIBIjANBgkq"],
        (f"_dmarc.{D}", "TXT"): ["v=DMARC1; p=quarantine; rua=mailto:d@acme.example"],
    }


def fake_resolver(records: dict):
    """Missing key -> NXDOMAIN (LookupError); an Exception value -> failed lookup."""
    calls = []

    def resolve(name, rdtype):
        calls.append((name, rdtype))
        value = records.get((name, rdtype))
        if value is None:
            raise LookupError(name)
        if isinstance(value, Exception):
            raise value
        return value

    resolve.calls = calls
    return resolve


def rdap(registered="2020-01-01T00:00:00Z"):
    return lambda domain: {"events": [{"eventAction": "registration", "eventDate": registered}]}


def run(records=None, **kw):
    kw.setdefault("rdap_fetch", rdap())
    return check_domain(D, resolver=fake_resolver(healthy() if records is None else records),
                        now=NOW, **kw)


def check(report, name):
    return next(c for c in report.checks if c.name == name)


# ---------------------------------------------------------------- happy path

def test_ready_happy_path():
    r = run()
    assert r.verdict == "READY" and r.reasons == []
    assert r.domain == D
    assert [c.name for c in r.checks] == ["mx", "spf", "dkim", "dmarc", "domain_age",
                                          "blocklist:dbl.spamhaus.org", "blocklist:multi.surbl.org"]
    assert all(c.status == "pass" for c in r.checks), r.checks


def test_domain_is_normalized_and_validated():
    assert check_domain("https://www.Acme.example/x", resolver=fake_resolver(healthy()),
                        rdap_fetch=rdap(), now=NOW).domain == D
    with pytest.raises(ValueError):
        check_domain("not a domain", resolver=fake_resolver({}), rdap_fetch=rdap(), now=NOW)


# ---------------------------------------------------------------- MX

def test_no_mx_fails():
    recs = healthy()
    del recs[(D, "MX")]
    r = run(recs)
    assert check(r, "mx").status == "fail" and r.verdict == "NOT READY"


def test_null_mx_fails():
    recs = healthy()
    recs[(D, "MX")] = ["0 ."]
    assert check(run(recs), "mx").status == "fail"


# ---------------------------------------------------------------- SPF

def spf(record, extra=None):
    recs = healthy()
    recs[(D, "TXT")] = [record] if isinstance(record, str) else record
    recs.update(extra or {})
    return check(run(recs), "spf")


def test_spf_softfail_passes():
    assert spf("v=spf1 include:_spf.google.com ~all").status == "pass"


def test_spf_hardfail_passes():
    assert spf("v=spf1 mx -all").status == "pass"


@pytest.mark.parametrize("record", ["v=spf1 +all", "v=spf1 mx all"])
def test_spf_plus_all_fails(record):
    c = spf(record)
    assert c.status == "fail" and "anyone" in c.detail


def test_spf_two_records_fails():
    c = spf(["v=spf1 mx ~all", "v=spf1 include:_spf.google.com ~all"])
    assert c.status == "fail" and "2 SPF records" in c.detail


def test_spf_missing_fails():
    assert spf(["google-site-verification=x"]).status == "fail"


def test_spf_neutral_and_no_all_warn():
    assert spf("v=spf1 mx ?all").status == "warn"
    assert spf("v=spf1 mx").status == "warn"


def test_spf_eleven_lookups_through_nested_includes_fails():
    # top: 2 includes + mx = 3; a: 4 lookups (+ include b); b: 4 lookups -> 3 + 4 + 4 = 11
    extra = {
        ("a.example", "TXT"): ["v=spf1 a mx ptr include:b.example ~all"],
        ("b.example", "TXT"): ["v=spf1 a mx exists:x.example include:c.example ~all"],
        ("c.example", "TXT"): ["v=spf1 ip4:10.0.0.0/8 ~all"],
    }
    c = spf("v=spf1 mx include:a.example include:c.example ~all", extra)
    assert c.status == "fail" and "11 DNS lookups" in c.detail


def test_spf_ten_lookups_passes():
    extra = {
        ("a.example", "TXT"): ["v=spf1 a mx ptr include:b.example ~all"],
        ("b.example", "TXT"): ["v=spf1 a mx exists:x.example ~all"],
        ("c.example", "TXT"): ["v=spf1 ip4:10.0.0.0/8 ~all"],
    }
    c = spf("v=spf1 mx include:a.example include:c.example ~all", extra)
    assert c.status == "pass" and "10 of 10" in c.detail


def test_spf_include_loop_terminates():
    extra = {("a.example", "TXT"): ["v=spf1 include:acme.example ~all"]}
    recs = healthy()
    recs[(D, "TXT")] = ["v=spf1 include:a.example ~all"]
    recs.update(extra)
    assert check(run(recs), "spf").status == "pass"


def test_spf_failed_include_lookup_is_unknown():
    c = spf("v=spf1 include:a.example ~all", {("a.example", "TXT"): Timeout()})
    assert c.status == "unknown"


# ---------------------------------------------------------------- DKIM

def test_dkim_found_on_default_selector():
    recs = healthy()
    del recs[(f"google._domainkey.{D}", "TXT")]
    recs[(f"snov._domainkey.{D}", "TXT")] = ["v=DKIM1; k=rsa; p=ABC"]
    c = check(run(recs), "dkim")
    assert c.status == "pass" and "snov" in c.detail


def test_dkim_not_found_with_default_selectors_warns():
    recs = healthy()
    del recs[(f"google._domainkey.{D}", "TXT")]
    r = run(recs)
    assert check(r, "dkim").status == "warn"
    assert r.verdict == "READY"


def test_dkim_not_found_with_explicit_selector_fails():
    r = run(dkim_selectors=["custom"])
    assert check(r, "dkim").status == "fail" and r.verdict == "NOT READY"


def test_dkim_found_with_explicit_selector():
    recs = healthy()
    recs[(f"custom._domainkey.{D}", "TXT")] = ["v=DKIM1; p=XYZ"]
    resolver = fake_resolver(recs)
    r = check_domain(D, dkim_selectors=["custom"], resolver=resolver, rdap_fetch=rdap(), now=NOW)
    assert check(r, "dkim").status == "pass"
    assert (f"google._domainkey.{D}", "TXT") not in resolver.calls


def test_dkim_revoked_key_is_not_a_key():
    recs = healthy()
    recs[(f"google._domainkey.{D}", "TXT")] = ["v=DKIM1; p="]
    assert check(run(recs), "dkim").status == "warn"


def test_dkim_failed_lookup_is_unknown_and_does_not_block():
    recs = healthy()
    recs[(f"google._domainkey.{D}", "TXT")] = Timeout()
    r = run(recs)
    assert check(r, "dkim").status == "unknown"
    assert r.verdict == "READY"


# ---------------------------------------------------------------- DMARC

def dmarc(value):
    recs = healthy()
    if value is None:
        del recs[(f"_dmarc.{D}", "TXT")]
    else:
        recs[(f"_dmarc.{D}", "TXT")] = value
    return run(recs)


def test_dmarc_missing_fails():
    r = dmarc(None)
    assert check(r, "dmarc").status == "fail"
    assert r.verdict == "NOT READY"
    assert any("no DMARC record" in reason and "p=none" in reason for reason in r.reasons)


def test_dmarc_none_warns():
    r = dmarc(["v=DMARC1; p=none"])
    assert check(r, "dmarc").status == "warn" and r.verdict == "READY"


@pytest.mark.parametrize("policy", ["quarantine", "reject", "Reject"])
def test_dmarc_enforcing_passes(policy):
    assert check(dmarc([f"v=DMARC1; p={policy}; pct=100"]), "dmarc").status == "pass"


def test_dmarc_two_records_fail():
    assert check(dmarc(["v=DMARC1; p=none", "v=DMARC1; p=reject"]), "dmarc").status == "fail"


# ---------------------------------------------------------------- failed lookups

@pytest.mark.parametrize("key,name", [((D, "MX"), "mx"), ((D, "TXT"), "spf"),
                                      ((f"_dmarc.{D}", "TXT"), "dmarc")])
def test_failed_core_lookup_is_unknown_and_not_ready(key, name):
    recs = healthy()
    recs[key] = Timeout()
    r = run(recs)
    c = check(r, name)
    assert c.status == "unknown"
    assert not any(x.status == "fail" for x in r.checks)
    assert r.verdict == "NOT READY" and c.detail in r.reasons


# ---------------------------------------------------------------- domain age

def test_young_domain_fails():
    r = run(rdap_fetch=rdap("2026-10-10T00:00:00Z"))
    c = check(r, "domain_age")
    assert c.status == "fail" and "13 days" in c.detail and r.verdict == "NOT READY"


def test_domain_under_90_days_warns():
    assert check(run(rdap_fetch=rdap("2026-08-01T00:00:00Z")), "domain_age").status == "warn"


def test_rdap_failure_is_unknown_and_does_not_block():
    def boom(domain):
        raise Timeout()

    r = run(rdap_fetch=boom)
    assert check(r, "domain_age").status == "unknown" and r.verdict == "READY"


def test_rdap_without_registration_event_is_unknown():
    r = run(rdap_fetch=lambda d: {"events": [{"eventAction": "expiration", "eventDate": "2030"}]})
    assert check(r, "domain_age").status == "unknown"


# ---------------------------------------------------------------- blocklists

def test_blocklist_listed_fails():
    recs = healthy()
    recs[(f"{D}.dbl.spamhaus.org", "A")] = ["127.0.1.2"]
    r = run(recs)
    assert check(r, "blocklist:dbl.spamhaus.org").status == "fail"
    assert r.verdict == "NOT READY"


def test_spamhaus_refused_resolver_is_unknown():
    recs = healthy()
    recs[(f"{D}.dbl.spamhaus.org", "A")] = ["127.255.255.254"]
    r = run(recs)
    c = check(r, "blocklist:dbl.spamhaus.org")
    assert c.status == "unknown" and "check.spamhaus.org" in c.detail
    assert r.verdict == "READY"


def test_surbl_refused_resolver_is_unknown():
    recs = healthy()
    recs[(f"{D}.multi.surbl.org", "A")] = ["127.0.0.1"]
    assert check(run(recs), "blocklist:multi.surbl.org").status == "unknown"


def test_blocklist_failed_lookup_is_unknown():
    recs = healthy()
    recs[(f"{D}.multi.surbl.org", "A")] = Timeout()
    assert check(run(recs), "blocklist:multi.surbl.org").status == "unknown"


# ---------------------------------------------------------------- default adapters

def test_default_resolver_maps_nxdomain_to_lookup_error(monkeypatch):
    import dns.resolver

    def nx(*a, **kw):
        raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr(dns.resolver, "resolve", nx)
    with pytest.raises(LookupError):
        warmup.dns_resolver(D, "TXT")


def test_default_resolver_lets_timeouts_through(monkeypatch):
    import dns.exception
    import dns.resolver

    def slow(*a, **kw):
        raise dns.exception.Timeout()

    monkeypatch.setattr(dns.resolver, "resolve", slow)
    with pytest.raises(dns.exception.Timeout):
        warmup.dns_resolver(D, "TXT")


# ---------------------------------------------------------------- ramp

RAMP = dict(warmup_start="2026-10-01", warmup_min_days=14, start_sends=5, daily_increase=3,
            daily_cap=30, ready_state="ready", paused=False)


def cap(day: int, **kw) -> int:
    args = {**RAMP, **kw}
    start = date.fromisoformat(args["warmup_start"]) if args["warmup_start"] else date(2026, 10, 1)
    return effective_daily_cap(**args, on_date=date.fromordinal(start.toordinal() + day))


def test_ramp_start_date_is_day_zero():
    assert cap(0) == 0


def test_ramp_day_13_sends_nothing():
    assert cap(13) == 0


def test_ramp_day_14_sends_start_sends():
    assert cap(14) == 5


def test_ramp_day_15_adds_daily_increase():
    assert cap(15) == 8


def test_ramp_reaches_and_stays_at_cap():
    # 5 + 3*(day-14) >= 30 from day 23 (5+27=32 -> 30); day 22 is 29
    assert cap(22) == 29
    assert cap(23) == 30
    assert cap(400) == 30


def test_ramp_before_start_is_zero():
    assert cap(-3) == 0


@pytest.mark.parametrize("kw", [{"paused": True}, {"ready_state": "not_ready"},
                                {"ready_state": "unknown"}, {"warmup_start": None}])
def test_ramp_blocked(kw):
    assert cap(30, **kw) == 0


def test_ramp_accepts_datetime_start():
    assert effective_daily_cap(**{**RAMP, "warmup_start": "2026-10-01T23:30:00+00:00"},
                               on_date=date(2026, 10, 15)) == 5
