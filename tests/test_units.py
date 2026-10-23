"""Unit tests for the decision modules: observations, email patterns, personas, scoring helpers,
KB isolation, schedule, forget, feedback and holdout."""

from __future__ import annotations

from datetime import timedelta

import pytest

from gtm import config, email_finder, enrich, fake, kb, ledger, observations as obs, report, schedule, store
from gtm.util import now

from tests.test_pipeline import SLUG, make_product


@pytest.fixture
def product(monkeypatch):
    monkeypatch.setenv("GTM_FAKE", "1")
    fake.reset()
    kb.MemoryBackend._stores.clear()
    kb._open.clear()
    make_product(SLUG)
    return store.open_state(SLUG)


# --- observations ------------------------------------------------------------------------------

def test_unconfirmed_code_is_skipped(product):
    product.execute("UPDATE signal_codes SET status = 'proposed' WHERE code = 'FUNDING'")
    assert obs.record(product, account_domain="x.example", signal_code="FUNDING", collector="t",
                      evidence_url="https://x.example", confidence=0.9) is None
    assert product.execute("SELECT count(*) FROM observations WHERE signal_code = 'FUNDING'").fetchone()[0] == 0


def test_threshold_confidence_and_latest_value(product):
    rec = lambda **kw: obs.record(product, account_domain="t.example", collector="t", evidence_url="https://t.example", **kw)
    rec(signal_code="SITE_PAGE_COUNT", value_num=50, confidence=0.9, observed_at=(now() - timedelta(days=5)).isoformat())
    assert obs.cohort(product, must=["SITE_PAGE_COUNT"]) == ["t.example"]
    rec(signal_code="SITE_PAGE_COUNT", value_num=5, confidence=0.9)  # newer value below threshold wins
    assert obs.cohort(product, must=["SITE_PAGE_COUNT"]) == []
    rec(signal_code="INDUSTRY", value_text="e-commerce", confidence=0.2)  # under min_confidence
    assert "INDUSTRY" not in {o.signal_code for o in obs.latest(product, "t.example")}
    assert obs.cohort(product, must_not=["INDUSTRY"], domains=["t.example"]) == ["t.example"]


def test_snapshot_codes_drop_removed_values(product):
    rec = lambda v, run: obs.record(product, account_domain="s.example", signal_code="TECH_STACK", collector="p",
                                    evidence_url="https://s.example", confidence=0.9, value_text=v, run_id=run)
    rec("Shopify", "r1"); rec("Klaviyo", "r1")
    rec("Shopify", "r2")
    stack = {o.value_text for o in obs.latest(product, "s.example") if o.signal_code == "TECH_STACK"}
    assert stack == {"Shopify"}


def test_unknown_code_rejected_by_database(product):
    with pytest.raises(Exception):
        product.execute("INSERT INTO observations (account_domain, collector, signal_code, confidence, evidence_url, observed_at) "
                        "VALUES ('a.example', 't', 'MADE_UP', 0.9, 'https://a.example', '2026-01-01')")


# --- email patterns ----------------------------------------------------------------------------

def test_email_pattern_inference_and_candidates():
    assert email_finder.infer("jane.doe@x.example", "Dr. Jane Doe") == "first.last"
    assert email_finder.infer("jdoe@x.example", "Jane Doe") == "flast"
    assert email_finder.local_part("first.last", "zoe", "") is None
    assert email_finder.split_name("José Álvarez") == ("jose", "alvarez")
    assert email_finder.shape_guess(["ann.bell@x", "cy.dodd@x", "info@x"], ("info",)) == "first.last"


def test_pattern_ttl_and_upgrade(product):
    email_finder.learn(product, "p.example", "first", "default")
    assert email_finder.get(product, "p.example")["pattern"] == "first"
    assert email_finder.learn(product, "p.example", "first.last", "verified")
    assert not email_finder.learn(product, "p.example", "flast", "domain_shape")  # lower confidence never replaces
    assert email_finder.get(product, "p.example")["pattern"] == "first.last"
    product.execute("UPDATE email_patterns SET expires_at = ? WHERE domain = 'p.example'",
                    ((now() - timedelta(days=1)).isoformat(),))
    assert email_finder.get(product, "p.example") is None


# --- personas ----------------------------------------------------------------------------------

def test_persona_match(product):
    personas = config.load(SLUG).personas
    assert enrich.match_persona("CEO", personas)[1] == "exact"
    assert enrich.match_persona("Head of Growth & Retention", personas)[0].name == "Head of Growth"
    assert enrich.match_persona("Executive Assistant to the CEO", personas) is None
    assert enrich.match_persona("Software Engineer", personas) is None
    assert enrich.match_persona(None, personas) is None


# --- KB ----------------------------------------------------------------------------------------

def test_kb_isolation_dedupe_and_pii(product):
    make_product("other")
    kb.add("other", "product_fact", "Other sells forklifts")
    assert all("forklift" not in m["text"] for m in kb.all_memories(SLUG))
    again = kb.add(SLUG, "product_fact", "Acme recovers abandoned Shopify carts with plain follow-up emails")
    assert again["status"] == "duplicate"
    with pytest.raises(kb.KBError):
        kb.add(SLUG, "voice", "call me on +91 98765 43210")
    with pytest.raises(kb.KBError):
        kb.add(SLUG, "proof", "Cut churn by 20%")


def test_human_fact_locked(product):
    m = kb.add(SLUG, "product_fact", "Pricing starts at $49 a month", authority="human")
    with pytest.raises(kb.HumanFactLocked):
        kb.delete(SLUG, m["id"])


# --- schedule ----------------------------------------------------------------------------------

def test_schedule_lines_are_tagged_and_converted(product, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    import time
    time.tzset()
    try:
        lines = schedule.lines_for(config.load(SLUG), feedback=True)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    assert all(line.endswith(f"# gtm-tools:{SLUG}") for line in lines)
    assert any(line.startswith("0 9 * * * ") and " full " in line for line in lines)
    assert any(line.startswith("17 * * * * ") and " sync " in line for line in lines)
    assert any(line.startswith("30 8 * * * ") and " feedback " in line for line in lines)


# --- scoring helpers ---------------------------------------------------------------------------

def test_wilson_interval():
    lo, hi = report.wilson(5, 100)
    assert 0.01 < lo < 0.05 < hi < 0.12
    assert report.wilson(0, 0) == (0.0, 0.0)


# --- flows over the fake world -----------------------------------------------------------------

def _full():
    from gtm import run
    res = run.execute(SLUG, "full", limit=5)
    assert res["status"] == "done", res


def test_holdout_skips_graph(product, monkeypatch):
    cfg = config.load(SLUG)
    cfg.scoring.holdout_pct = 50
    config.save(cfg)
    _full()
    rows = product.execute("SELECT verdict, holdout FROM scores").fetchall()
    held = [r for r in rows if r["holdout"] == 1]
    assert held and all(r["verdict"] == "keep" for r in held)  # holdout leads skip the reviewers, kept as-is


def test_competitor_never_prospected(product):
    _full()
    assert product.execute("SELECT count(*) FROM sequences s JOIN leads l USING (lead_key) "
                           "WHERE l.account_domain = 'rivalcart.example'").fetchone()[0] == 0


def test_forget_erases_and_suppresses(product):
    _full()
    lead = product.execute("SELECT * FROM leads WHERE email IS NOT NULL LIMIT 1").fetchone()
    from gtm import forget
    out = forget.forget(lead["email"])
    assert out["products"][SLUG]["leads"] == 1
    assert product.execute("SELECT count(*) FROM leads WHERE email = ?", (lead["email"],)).fetchone()[0] == 0
    assert ledger.is_suppressed(ledger.open_ledger(), email=lead["email"])


def test_feedback_referral_and_draft(product):
    _full()
    seq = product.execute("SELECT * FROM sequences LIMIT 1").fetchone()
    for i, body in enumerate(["Not me, talk to priya shah, she runs growth.", "Tell me more please"]):
        product.execute("INSERT INTO replies (id, seq_id, lead_key, received_at, body_stripped, status) "
                        "VALUES (?, ?, ?, ?, ?, 'new')", (f"r{i}", seq["seq_id"], seq["lead_key"], now().isoformat(), body))
    from gtm import feedback
    out = feedback.run(SLUG)
    assert out["classified"] == 2 and out["referrals"] == 1 and out["drafts"] == 2
    ref = product.execute("SELECT * FROM leads WHERE referral = 1").fetchone()
    assert ref["full_name"] == "Priya Shah"
    assert product.execute("SELECT count(*) FROM reply_drafts WHERE status = 'pending_review'").fetchone()[0] == 2
