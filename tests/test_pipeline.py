"""End-to-end on the fake world (GTM_FAKE=1): collect -> resolve -> enrich -> qualify -> write ->
review (signed) -> push, plus the plan's Phase 7/8 safety exit criteria."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from gtm import catalog, config, fake, kb, ledger, mailboxes, outbox, review, run, signing, store
from gtm.util import now

SLUG = "acme"
PIN = "correct-horse-9"


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("GTM_FAKE", "1")
    fake.reset()
    kb.MemoryBackend._stores.clear()
    kb._open.clear()
    make_product(SLUG)
    signing.init_keys(PIN)
    key = signing.unlock(PIN)
    led = ledger.open_ledger()
    mailboxes.add(led, key, mailbox_id="mb_1", address="ravi@getacme.example", daily_cap=30,
                  warmup_start=(now() - timedelta(days=40)).date().isoformat())
    led.execute("UPDATE mailboxes SET ready_state = 'ready' WHERE id = 'mb_1'")
    led.execute("INSERT INTO snov_campaigns (product, mailbox_id, list_id, campaign_id, settings_ok, created_at) "
                "VALUES (?, 'mb_1', 11, 22, 1, ?)", (SLUG, now().isoformat()))
    led.execute("INSERT INTO settings (key, value) VALUES ('snov_dne_list_id', '33')")
    return key


def make_product(slug: str, competitors=("RivalCart",)) -> None:
    cfg = config.ProductConfig.model_validate({
        "product": {"slug": slug, "name": "Acme", "url": "https://acme.example"},
        "icp": {"segments": [{"name": "D2C", "industries": ["e-commerce"], "size_band": ["11-50", "51-200"],
                              "geos": ["US"], "tech_stack_any": ["Shopify"]}]},
        "personas": [{"name": "Founder/CEO", "titles": ["CEO", "Founder", "Co-founder"], "seniority": "c_level"},
                     {"name": "Head of Growth", "titles": ["Head of Growth", "VP Marketing", "CMO"], "seniority": "vp"}],
        "signals": {"linkedin": {"keywords": ["cart recovery"]}, "competitors": list(competitors),
                    "hiring_keywords": ["CRO"]},
        "sending": {"mailboxes": [{"id": "mb_1", "share": 1.0}], "window": {"start": "00:00", "end": "23:59"}},
        "voice": {"sender_name": "Ravi Kumar", "postal_address": "221B MG Road, Bengaluru 560001, India"},
    })
    config.save(cfg)
    conn = store.open_state(slug)
    catalog.seed(conn)
    conn.execute("UPDATE signal_codes SET status = 'confirmed' WHERE code IN ('ASKS_FOR_SOLUTION','COMPETITOR_PAIN',"
                 "'HIRING_ROLE','HIRING_FOR_PROBLEM','INDUSTRY','SIZE_BAND','HQ_COUNTRY','TECH_STACK','SITE_PAGE_COUNT',"
                 "'FUNDING','LEADERSHIP_CHANGE','TECH_ADDED','TECH_REMOVED','SITE_CHANGE','PRICING_CHANGE')")
    kb.add(slug, "product_fact", "Acme recovers abandoned Shopify carts with plain follow-up emails")
    (config.config_path(slug).parent / "clients.csv").write_text("company,domain\nClientCo,clientco.example\n")
    from gtm import accounts
    accounts.import_clients(conn, slug)
    accounts.profile_accounts(conn, config.load(slug), ["clientco.example"])


def full_run():
    res = run.execute(SLUG, "full", limit=5)
    assert res["status"] == "done", res
    return res


def pending(conn):
    return conn.execute("SELECT * FROM sequences WHERE status = 'pending_review'").fetchall()


def test_full_run_writes_sequences_and_nothing_is_sent(world):
    res = full_run()
    conn = store.open_state(SLUG)
    seqs = pending(conn)
    assert len(seqs) >= 2, res
    domains = {conn.execute("SELECT account_domain FROM leads WHERE lead_key = ?", (s["lead_key"],)).fetchone()[0] for s in seqs}
    assert "brandly.example" in domains and "shopco.example" in domains
    assert "clientco.example" not in domains  # an existing client is never prospected
    assert fake.SENT == []  # nothing goes out without a signed approval
    # an anonymous post never becomes a lead, and an account only when the post names the company (§15.1 E1)
    assert conn.execute("SELECT count(*) FROM posts WHERE resolvability = 'anonymous' AND lead_key IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM posts WHERE resolvability = 'anonymous' AND account_domain IS NOT NULL "
                        "AND employer IS NULL").fetchone()[0] == 0
    # one primary contact per account
    assert conn.execute("SELECT max(n) FROM (SELECT count(*) n FROM sequences GROUP BY "
                        "(SELECT account_domain FROM leads l WHERE l.lead_key = sequences.lead_key))").fetchone()[0] == 1
    for s in seqs:
        assert json.loads(s["validator_json"]) == []
    assert res["counts"]["llm_calls"] <= config.load(SLUG).usage.limits()["llm_calls_per_run"]


def test_signed_approval_pushes_once(world):
    full_run()
    conn = store.open_state(SLUG)
    seq = pending(conn)[0]
    review.approve(config.load(SLUG), conn, SLUG, seq["seq_id"], world, approved_by="founder")
    res = run.execute(SLUG, "sync")
    assert res["status"] == "done", res
    assert len(fake.SENT) == 1
    sent = fake.SENT[0]
    assert sent["email"] == seq["recipient_email"]
    assert "221B MG Road" in sent["custom_fields"]["gtm_body_1"]
    assert conn.execute("SELECT status FROM sequences WHERE seq_id = ?", (seq["seq_id"],)).fetchone()[0] == "pushed"
    led = ledger.open_ledger()
    assert led.execute("SELECT status FROM account_sequences WHERE id = ?", (seq["seq_id"],)).fetchone()[0] == "active"
    assert led.execute("SELECT count(*) FROM reservations WHERE seq_id = ?", (seq["seq_id"],)).fetchone()[0] == 3
    run.execute(SLUG, "sync")
    assert len(fake.SENT) == 1  # never twice


def test_forged_approval_row_never_pushes(world):
    full_run()
    conn = store.open_state(SLUG)
    seq = pending(conn)[0]
    conn.execute("INSERT INTO approvals (target, target_id, content_hash, approved_by, approved_at, via, edited, signature) "
                 "VALUES ('sequence', ?, ?, 'mallory', ?, 'tty_pin', 0, 'AAAA')", (seq["seq_id"], seq["content_hash"], now().isoformat()))
    conn.execute("UPDATE sequences SET status = 'approved' WHERE seq_id = ?", (seq["seq_id"],))
    run.execute(SLUG, "sync")
    assert fake.SENT == []


def test_changed_footer_after_approval_blocks_push(world):
    full_run()
    conn = store.open_state(SLUG)
    seq = pending(conn)[0]
    review.approve(config.load(SLUG), conn, SLUG, seq["seq_id"], world, approved_by="founder")
    cfg = config.load(SLUG)
    cfg.voice.postal_address = "somewhere else 1"
    config.save(cfg)
    run.execute(SLUG, "sync")
    assert fake.SENT == []


def test_changed_lead_email_after_approval_blocks_push(world):
    full_run()
    conn = store.open_state(SLUG)
    seq = pending(conn)[0]
    review.approve(config.load(SLUG), conn, SLUG, seq["seq_id"], world, approved_by="founder")
    conn.execute("UPDATE leads SET email = 'other.person@brandly.example' WHERE lead_key = ?", (seq["lead_key"],))
    run.execute(SLUG, "sync")
    assert fake.SENT == []


def test_edit_after_approval_is_impossible_and_rewrite_needs_new_approval(world):
    full_run()
    conn = store.open_state(SLUG)
    seq = pending(conn)[0]
    cfg = config.load(SLUG)
    steps = review.from_text(review.to_text(__import__("gtm.sequence", fromlist=["x"]).steps_from_json(seq["steps_json"])))
    steps[1].body = steps[1].body.replace("one more thought", "a different thought")
    review.apply_edit(cfg, conn, seq["seq_id"], steps)
    edited = conn.execute("SELECT * FROM sequences WHERE seq_id = ?", (seq["seq_id"],)).fetchone()
    assert edited["content_hash"] != seq["content_hash"] and edited["edited"] == 1
    review.approve(cfg, conn, SLUG, seq["seq_id"], world, approved_by="founder")
    with pytest.raises(review.ReviewError):
        review.apply_edit(cfg, conn, seq["seq_id"], steps)
    assert conn.execute("SELECT edited FROM approvals WHERE target_id = ?", (seq["seq_id"],)).fetchone()[0] == 1


def test_push_exception_is_failed_maybe_sent_and_never_retried(world):
    full_run()
    conn = store.open_state(SLUG)
    seq = pending(conn)[0]
    review.approve(config.load(SLUG), conn, SLUG, seq["seq_id"], world, approved_by="founder")
    fake.SNOV_FAIL["add_prospect"] = True
    run.execute(SLUG, "sync")
    assert conn.execute("SELECT status FROM sequences WHERE seq_id = ?", (seq["seq_id"],)).fetchone()[0] == "failed_maybe_sent"
    fake.SNOV_FAIL["add_prospect"] = False
    run.execute(SLUG, "sync")
    assert fake.SENT == []
    assert ledger.open_ledger().execute("SELECT count(*) FROM manual_actions WHERE action = 'check_snov_push'").fetchone()[0] == 1


def test_failed_reply_pull_means_zero_pushes(world):
    full_run()
    conn = store.open_state(SLUG)
    seq = pending(conn)[0]
    review.approve(config.load(SLUG), conn, SLUG, seq["seq_id"], world, approved_by="founder")
    fake.SNOV_FAIL["replies"] = True
    res = run.execute(SLUG, "sync")
    assert fake.SENT == []
    assert any("no sends" in n or "nothing pushed" in n for n in res["notes"])


def test_double_claim_pushes_once(world):
    full_run()
    conn = store.open_state(SLUG)
    seq = pending(conn)[0]
    review.approve(config.load(SLUG), conn, SLUG, seq["seq_id"], world, approved_by="founder")
    row = conn.execute("SELECT * FROM sequences WHERE seq_id = ?", (seq["seq_id"],)).fetchone()

    class Ctx:  # two drains holding the same stale row
        pass
    from gtm.budget import Budget
    from gtm import llm
    ctx = run.Ctx(slug=SLUG, cfg=config.load(SLUG), conn=conn, ledger=ledger.open_ledger(), run_id="r1", mode="sync",
                  run_dir=store.state_db(SLUG).parent, llm=llm.Session(conn), budget=Budget(1, 1))
    client = fake.FakeSnov()
    outbox.push_one(ctx, row, client)
    with pytest.raises(outbox.PushRefused):
        outbox.push_one(ctx, row, client)
    assert len(fake.SENT) == 1


def test_opt_out_reply_suppresses_everywhere(world):
    full_run()
    conn = store.open_state(SLUG)
    seq = pending(conn)[0]
    review.approve(config.load(SLUG), conn, SLUG, seq["seq_id"], world, approved_by="founder")
    run.execute(SLUG, "sync")
    email = seq["recipient_email"]
    fake.REPLIES[22] = [{"prospectEmail": email, "replyType": "email",
                         "replies": [{"subject": "re", "receivedAt": "2026-10-24T10:00:00",
                                      "message": "STOP\n\nOn Mon, Ravi wrote:\n> Is cart recovery something..."}]}]
    run.execute(SLUG, "sync")
    led = ledger.open_ledger()
    assert ledger.is_suppressed(led, email=email)
    assert led.execute("SELECT status FROM account_sequences WHERE id = ?", (seq["seq_id"],)).fetchone()[0] == "opted_out"


def test_reply_quoting_our_footer_is_not_an_opt_out(world):
    full_run()
    conn = store.open_state(SLUG)
    seq = pending(conn)[0]
    review.approve(config.load(SLUG), conn, SLUG, seq["seq_id"], world, approved_by="founder")
    run.execute(SLUG, "sync")
    footer = config.load(SLUG).voice.opt_out_line
    fake.REPLIES[22] = [{"prospectEmail": seq["recipient_email"], "replyType": "email",
                         "replies": [{"subject": "re", "receivedAt": "2026-10-24T11:00:00",
                                      "message": f"Sounds interesting, tell me more.\n{footer}"}]}]
    run.execute(SLUG, "sync")
    assert not ledger.is_suppressed(ledger.open_ledger(), email=seq["recipient_email"])
    assert conn.execute("SELECT status FROM replies").fetchone()[0] == "new"


def test_bounce_burst_pauses_mailbox(world):
    full_run()
    conn = store.open_state(SLUG)
    seq = pending(conn)[0]
    review.approve(config.load(SLUG), conn, SLUG, seq["seq_id"], world, approved_by="founder")
    run.execute(SLUG, "sync")
    fake.ACTIVITY[22] = [{"event_type": "sent", "recipient_email": seq["recipient_email"], "event_time": "2026-10-23 10:00"},
                         {"event_type": "bounce", "recipient_email": seq["recipient_email"], "event_time": "2026-10-23 10:05"}]
    run.execute(SLUG, "sync")
    led = ledger.open_ledger()
    assert led.execute("SELECT paused FROM mailboxes WHERE id = 'mb_1'").fetchone()[0] == 1
    assert ledger.is_suppressed(led, email=seq["recipient_email"])


def test_kill_switch_blocks_pushes(world):
    full_run()
    conn = store.open_state(SLUG)
    seq = pending(conn)[0]
    review.approve(config.load(SLUG), conn, SLUG, seq["seq_id"], world, approved_by="founder")
    ledger.open_ledger().execute("UPDATE sending_state SET paused = 1 WHERE id = 1")
    run.execute(SLUG, "sync")
    assert fake.SENT == []


def test_mailbox_inside_warmup_pushes_nothing(world):
    full_run()
    conn = store.open_state(SLUG)
    seq = pending(conn)[0]
    review.approve(config.load(SLUG), conn, SLUG, seq["seq_id"], world, approved_by="founder")
    led = ledger.open_ledger()
    key = world
    mailboxes.add(led, key, mailbox_id="mb_1", address="ravi@getacme.example", daily_cap=30,
                  warmup_start=(now() - timedelta(days=3)).date().isoformat())
    run.execute(SLUG, "sync")
    assert fake.SENT == []


def test_unsigned_mailbox_limit_change_sends_nothing(world):
    full_run()
    conn = store.open_state(SLUG)
    seq = pending(conn)[0]
    review.approve(config.load(SLUG), conn, SLUG, seq["seq_id"], world, approved_by="founder")
    ledger.open_ledger().execute("UPDATE mailboxes SET daily_cap = 200 WHERE id = 'mb_1'")
    run.execute(SLUG, "sync")
    assert fake.SENT == []


def test_resume_reuses_external_refs(world):
    """A killed run resumes the same run_id; recorded Apify/Snov refs are reused, not re-bought."""
    conn = store.open_state(SLUG)
    conn.execute("INSERT INTO runs (run_id, mode, started_at, status, attempt) VALUES ('20261023-090000-dead', 'full', ?, 'running', 1)",
                 (now().isoformat(),))
    conn.execute("INSERT INTO steps (run_id, step, status, external_ref) VALUES ('20261023-090000-dead', 'collect', 'done', '{}')")
    fake.FakeApify.started.clear()
    res = run.execute(SLUG, "full", limit=5)
    assert res["run_id"] == "20261023-090000-dead" and res["resumed"] and res["attempt"] == 2
    assert not any(kind in ("posts", "jobs") for kind, _ in fake.FakeApify.started)  # collect was done: skipped


def test_two_products_share_a_mailbox_without_exceeding_its_cap(world):
    led = ledger.open_ledger()
    led.execute("UPDATE mailboxes SET daily_cap = 3 WHERE id = 'mb_1'")
    mailboxes.add(led, world, mailbox_id="mb_1", address="ravi@getacme.example", daily_cap=3,
                  warmup_start=(now() - timedelta(days=60)).date().isoformat(), start_sends=3)
    days = mailboxes.touch_days([4, 12])
    ok = 0
    for i in range(6):
        led.execute("BEGIN IMMEDIATE")
        good, _ = mailboxes.reserve(led, mailbox_id="mb_1", seq_id=f"s{i}", days=days)
        led.execute("COMMIT")
        ok += good
    assert ok == 3
    assert mailboxes.touches_on(led, "mb_1", days[0]) == 3


def test_india_blocked_without_legal_signoff(world):
    from gtm import gates
    cfg = config.load(SLUG)
    ok, reason, _ = gates.geo_allowed(cfg, lead_country="IN", hq_country=None, domain="x.in")
    assert not ok
    with pytest.raises(Exception):
        config.ProductConfig.model_validate({**cfg.model_dump(mode="json"),
                                             "geo_policy": {"IN": {"status": "allowed"}, "default": {"status": "blocked"}}})
