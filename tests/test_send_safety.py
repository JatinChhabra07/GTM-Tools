"""Regression tests for the independent send-safety review (2026-10-23): each case once let an email
go out that shouldn't have, or let a brake be lifted without the founder's PIN."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from gtm import config, fake, forget, gates, ledger, mailboxes, outbox, review, run, sequence, signing, store, validate
from gtm.util import now, now_iso

from tests.test_pipeline import SLUG, full_run, pending, world  # noqa: F401  (fixture)


def _approve_first(key):
    conn = store.open_state(SLUG)
    seq = pending(conn)[0]
    review.approve(config.load(SLUG), conn, SLUG, seq["seq_id"], key, approved_by="founder")
    return conn, seq


@pytest.mark.parametrize("where", ["Vienna, Austria", "Warsaw, Poland", "Brussels, Belgium", "Paris",
                                   "The Netherlands", "Deutschland", "Somewhere Unparseable"])
def test_unreadable_or_eu_lead_location_never_falls_back_to_us_hq(world, where):
    ok, reason, _ = gates.geo_allowed(config.load(SLUG), lead_country=where, hq_country="US", domain="x.com")
    assert not ok, reason


def test_known_us_locations_still_pass(world):
    for where in ("San Francisco Bay Area", "Austin, Texas, United States", "New York, NY", "Texas"):
        ok, reason, _ = gates.geo_allowed(config.load(SLUG), lead_country=where, hq_country=None, domain="x.com")
        assert ok, (where, reason)
    ok, _, _ = gates.geo_allowed(config.load(SLUG), lead_country=None, hq_country="US", domain="x.com")
    assert ok  # no lead location at all: the company's HQ decides
    assert gates.country_code("Bengaluru") == "IN"


def test_sequence_must_have_exactly_three_emails(world):
    full_run()
    conn = store.open_state(SLUG)
    seq = pending(conn)[0]
    steps = sequence.steps_from_json(seq["steps_json"])[:2]
    lead = conn.execute("SELECT * FROM leads WHERE lead_key = ?", (seq["lead_key"],)).fetchone()
    account = conn.execute("SELECT * FROM accounts WHERE domain = ?", (lead["account_domain"],)).fetchone()
    vctx = sequence.context(config.load(SLUG), seq=seq, lead=lead, account=account, notice=seq["notice"])
    assert any(v.rule == "sequence_shape" for v in validate.validate_sequence(steps, vctx))
    assert review.apply_edit(config.load(SLUG), conn, seq["seq_id"], steps)  # saved, flagged
    with pytest.raises(review.ReviewError):  # and never signable
        review.approve(config.load(SLUG), conn, SLUG, seq["seq_id"], world, approved_by="founder")


def test_kill_switch_raw_unpause_does_not_resume(world):
    full_run()
    _approve_first(world)
    led = ledger.open_ledger()
    led.execute("UPDATE sending_state SET paused = 1 WHERE id = 1")
    led.execute("UPDATE sending_state SET paused = 0 WHERE id = 1")  # no PIN, no signature
    run.execute(SLUG, "sync")
    assert fake.SENT == []
    # the founder's signed resume works
    at = now_iso()
    sig = signing.sign(world, signing.resume_payload(at=at, approved_by="founder"))
    led.execute("UPDATE sending_state SET at = ?, resume_signature = ? WHERE id = 1",
                (at, json.dumps({"by": "founder", "sig": sig})))
    run.execute(SLUG, "sync")
    assert len(fake.SENT) == 1


def test_bounce_brake_needs_founder_resign(world):
    led = ledger.open_ledger()
    led.execute("UPDATE mailboxes SET paused = 1, brake_count = brake_count + 1 WHERE id = 'mb_1'")
    led.execute("UPDATE mailboxes SET paused = 0 WHERE id = 'mb_1'")  # raw un-pause
    row = mailboxes.get(led, "mb_1")
    assert not mailboxes.signed_ok(row) and mailboxes.cap_on(row, now().date()) == 0
    mailboxes.add(led, world, mailbox_id="mb_1", address="ravi@getacme.example", daily_cap=30,
                  warmup_start=(now() - timedelta(days=40)).date().isoformat())
    row = mailboxes.get(led, "mb_1")
    assert mailboxes.signed_ok(row) and not row["paused"]


def test_forget_suppresses_even_if_a_purge_fails(world, monkeypatch):
    def boom(slug, email):
        raise OSError("disk full")
    monkeypatch.setattr(forget, "forget_in_product", boom)
    with pytest.raises(OSError):
        forget.forget("jane.doe@brandly.example")
    assert ledger.is_suppressed(ledger.open_ledger(), email="jane.doe@brandly.example")


def test_policy_approval_never_covers_edited_content(world):
    full_run()
    conn = store.open_state(SLUG)
    seq = pending(conn)[0]
    led = ledger.open_ledger()
    payload = signing.autosend_payload(product=SLUG, enabled_at=now_iso(), approved_by="founder")
    led.execute("INSERT INTO autosend (product, enabled, enabled_at, signature) VALUES (?, 1, ?, ?)",
                (SLUG, now_iso(), json.dumps({"payload": payload, "sig": signing.sign(world, payload)})))
    conn.execute("INSERT INTO approvals (target, target_id, content_hash, approved_by, approved_at, via, edited, "
                 "signature) VALUES ('sequence', ?, ?, 'policy:autosend', ?, 'policy', 0, NULL)",
                 (seq["seq_id"], seq["content_hash"], now_iso()))
    assert outbox.approval_for(conn, led, product=SLUG, target="sequence", target_id=seq["seq_id"],
                               content_hash=seq["content_hash"], score=99, edited=True) is None
    assert outbox.approval_for(conn, led, product=SLUG, target="sequence", target_id=seq["seq_id"],
                               content_hash=seq["content_hash"], score=99, edited=False) is not None


def test_autosend_switches_itself_off_on_bounces(world):
    led = ledger.open_ledger()
    payload = signing.autosend_payload(product=SLUG, enabled_at=now_iso(), approved_by="founder")
    led.execute("INSERT INTO autosend (product, enabled, enabled_at, signature) VALUES (?, 1, ?, ?)",
                (SLUG, now_iso(), json.dumps({"payload": payload, "sig": signing.sign(world, payload)})))
    for i, st in enumerate(["active"] * 9 + ["bounced"]):
        led.execute("INSERT INTO account_sequences (id, product, lead_key, email_hash, domain, mailbox_id, status, "
                    "started_at) VALUES (?, ?, 'lk', ?, ?, 'mb_1', ?, ?)", (f"s{i}", SLUG, f"h{i}", f"d{i}.example", st, now_iso()))
    assert outbox.guardrails_red(led, SLUG)
    run.execute(SLUG, "sync")
    assert not outbox.autosend_active(led, SLUG)


def test_window_parses_single_digit_hours(world):
    cfg = config.load(SLUG)
    cfg.sending.window = {"start": "0:00", "end": "23:59"}
    assert outbox.in_window(cfg)


def test_add_prospect_refreshes_custom_fields(world, monkeypatch):
    from gtm.sources import snov
    sent = {}
    monkeypatch.setattr(snov.Snov, "__init__", lambda self: None)
    monkeypatch.setattr(snov.Snov, "request", lambda self, m, path, **kw: sent.update(kw.get("data", {})) or {"success": True})
    snov.Snov().add_prospect(1, email="a@b.co", first_name="A", last_name="B", company="C", position=None,
                             custom_fields={"gtm_body_1": "x"})
    assert sent["updateContact"] == "true" and sent["createDuplicates"] == "false"


def test_replies_paging_reads_until_empty(world, monkeypatch):
    from gtm.sources import snov
    pages = {0: [{"id": i} for i in range(20)], 20: [{"id": 99}], 21: []}
    monkeypatch.setattr(snov.Snov, "__init__", lambda self: None)
    monkeypatch.setattr(snov.Snov, "request", lambda self, m, path, params=None, **kw: {"data": pages[params["offset"]]})
    assert len(snov.Snov().replies(5)) == 21
