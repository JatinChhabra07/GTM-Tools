"""Phase D3 (plan §15.1 E1): source test, intent sheet, company-named posts, fallback sources."""

from __future__ import annotations

import csv
import json
import os
from datetime import timedelta

from typer.testing import CliRunner

from gtm import config, fake, llm, run, signals, social, store
from gtm.budget import Budget
from gtm.cli import app
from gtm.ledger import open_ledger
from gtm.sources import apify, last30days
from gtm.util import now

from tests.test_pipeline import SLUG, world  # noqa: F401  (fixture)

REDDIT_ANON = "https://www.reddit.com/r/shopify/comments/abc123/cart_tool/"
REDDIT_NAMED_CO = "https://www.reddit.com/r/ecommerce/comments/def456/brandly_carts/"


def _sources(**switches) -> None:
    cfg = config.load(SLUG)
    cfg.signals.sources = switches
    config.save(cfg)


def _full_run() -> dict:
    res = run.execute(SLUG, "full", limit=5)
    assert res["status"] == "done", res
    return res


def _ctx(conn, tmp_path, cap: int = 60) -> run.Ctx:
    return run.Ctx(slug=SLUG, cfg=config.load(SLUG), conn=conn, ledger=open_ledger(), run_id="t-run", mode="full",
                   run_dir=tmp_path, llm=llm.Session(conn, run_id="t-run", cap=cap), budget=Budget(3, 60))


def test_anonymous_reddit_post_lands_in_the_sheet_and_never_in_leads(world):
    res = _full_run()
    rows = {r["post_url"]: r for r in social.read_sheet(SLUG)}
    r = rows[REDDIT_ANON]
    assert (r["source"], r["intent_code"], r["author_profile_url"]) == \
        ("reddit", "ASKS_FOR_SOLUTION", "https://www.reddit.com/user/cartwoes_22")
    assert r["summary"] == "wants a cart recovery tool" and r["date"] == now().date().isoformat()
    hn = rows["https://news.ycombinator.com/item?id=4242"]  # the discussion, not the linked article
    assert hn["author_profile_url"] == "https://news.ycombinator.com/user?id=hnbuyer"
    assert rows[REDDIT_NAMED_CO]["company_if_named"] == "Brandly" and rows[REDDIT_NAMED_CO]["author_profile_url"] == ""
    assert "https://www.linkedin.com/posts/sam-carter-1" not in rows  # a named person is a lead, not a sheet row
    assert os.stat(social.sheet_path(SLUG)).st_mode & 0o777 == 0o600
    conn = store.open_state(SLUG)
    community = conn.execute("SELECT * FROM posts WHERE source IN ('reddit','hackernews')").fetchall()
    assert len(community) == 4  # 3 intent posts + one plain complaint (pain language only)
    assert all(p["resolvability"] == "anonymous" and p["lead_key"] is None and p["author_name"] is None
               for p in community)
    assert not conn.execute("SELECT 1 FROM leads WHERE linkedin_url LIKE '%reddit%' OR linkedin_url LIKE '%ycombinator%' "
                            "OR full_name LIKE '%cartwoes%' OR full_name LIKE '%hnbuyer%'").fetchone()
    hn_text = conn.execute("SELECT text_excerpt FROM posts WHERE source = 'hackernews'").fetchone()[0]
    assert not hn_text.endswith("Great question")  # commenters' words are cut before labelling
    assert res["counts"]["intent_sheet_rows"] == 3
    report = open(res["report"]).read()
    assert "3 new row(s) in the intent sheet" in report and str(social.sheet_path(SLUG)) in report
    # pain language still comes from the same pass
    assert res["counts"]["pain_phrases"] >= 0 and fake.SENT == []


def test_sheet_dedupes_by_post_url_and_neutralises_formulas(world):
    row = social.sheet_row(source="quora", post_url="https://www.quora.com/q1", author_profile_url="javascript:x",
                           intent_code="ASKS_FOR_SOLUTION", summary="=HYPERLINK(evil) wants a tool, mail a@b.co",
                           company="-Acme")
    assert social.add_rows(SLUG, [row, dict(row)]) == 1
    assert social.add_rows(SLUG, [dict(row)]) == 0
    with open(social.sheet_path(SLUG), newline="") as f:
        written = list(csv.DictReader(f))
    assert len(written) == 1
    w = written[0]
    assert w["author_profile_url"] == ""  # only http(s) links
    assert w["summary"] == ""  # contained an email address: dropped
    assert w["company_if_named"] == "'-Acme"  # a leading - would be read as a formula
    assert not any(v.startswith(("=", "+", "@")) for v in w.values())


def test_company_named_post_creates_an_account_but_no_lead(world):
    started = len(fake.FakeApify.started)
    _sources(linkedin=False)  # so Brandly is only known from the Reddit post
    _full_run()
    conn = store.open_state(SLUG)
    acct = conn.execute("SELECT * FROM accounts WHERE domain = 'brandly.example'").fetchone()
    assert acct and acct["source"] == "signal"
    ob = conn.execute("SELECT * FROM observations WHERE collector = 'post:reddit'").fetchall()
    assert len(ob) == 1 and ob[0]["evidence_url"] == REDDIT_NAMED_CO and ob[0]["lead_key"] is None
    post = conn.execute("SELECT * FROM posts WHERE url = ?", (REDDIT_NAMED_CO,)).fetchone()
    assert post["account_domain"] == "brandly.example" and post["lead_key"] is None
    assert post["resolvability"] == "anonymous"
    assert conn.execute("SELECT count(*) FROM leads WHERE is_signal_author = 1").fetchone()[0] == 0
    # the anonymous post without a company stays unresolved
    assert conn.execute("SELECT account_domain FROM posts WHERE url = ?", (REDDIT_ANON,)).fetchone()[0] is None
    assert "posts" not in [k for k, _ in fake.FakeApify.started[started:]]  # LinkedIn was switched off


def test_disabled_sources_are_not_collected_and_enabled_fallbacks_are(world):
    _sources(reddit=False, hackernews=False, quora=True)
    res = _full_run()
    conn = store.open_state(SLUG)
    sources = {r[0] for r in conn.execute("SELECT DISTINCT source FROM posts")}
    assert "reddit" not in sources and "hackernews" not in sources
    assert "pain_phrases" not in res["counts"]  # last30days didn't run at all
    q = conn.execute("SELECT * FROM posts WHERE source = 'quora'").fetchone()
    assert q["resolvability"] == "anonymous" and q["intent_code"] == "ASKS_FOR_SOLUTION"
    assert "https://www.quora.com/What-is-a-good-cart-recovery-tool" in {r["post_url"] for r in social.read_sheet(SLUG)}
    assert "indiehackers" not in sources  # not enabled: fallbacks are off by default


def test_default_switches_keep_current_behaviour():
    cfg = config.ProductConfig.model_validate({"product": {"slug": "p1", "name": "P", "url": "https://p.example"}})
    assert [s for s in config.SOCIAL_SOURCES if social.source_on(cfg, s)] == ["linkedin", "x", "reddit", "hackernews"]
    cfg.signals.instagram = {"enabled": True}
    assert social.source_on(cfg, "instagram")
    try:
        config.ProductConfig.model_validate({"product": {"slug": "p1", "name": "P", "url": "https://p.example"},
                                             "signals": {"sources": {"myspace": True}}})
        raise AssertionError("unknown source accepted")
    except ValueError:
        pass


def test_source_with_zero_yield_is_recommended_off(world):
    started = len(fake.FakeApify.started)
    conn = store.open_state(SLUG)
    res = social.source_test(SLUG, config.load(SLUG), conn, confirm=False)
    by = {r["source"]: r for r in res["results"]}
    assert by["reddit"]["recommend"] == "on" and by["reddit"]["intent"] == 2
    assert by["reddit"]["share_resolvable"] == 0.5  # one of two names the company
    assert by["hackernews"]["recommend"] == "on" and by["quora"]["recommend"] == "on"
    assert by["jobs"]["items"] == 0 and by["jobs"]["recommend"] == "off"
    assert by["indiehackers"]["items"] == 1 and by["indiehackers"]["recommend"] == "off"  # posts, but no intent
    assert by["linkedin"]["status"] == "needs_confirm" and by["linkedin"]["recommend"] == "unknown"
    assert by["linkedin"]["estimate_usd"] > 0
    assert len(fake.FakeApify.started) == started  # nothing paid without --confirm
    assert res["isolated_calls"] <= social.TEST_CALL_CAP
    saved = json.loads(open(res["path"]).read())
    assert saved["tested_at"] and len(saved["results"]) == len(config.SOCIAL_SOURCES)
    assert os.stat(res["path"]).st_mode & 0o777 == 0o600
    assert conn.execute("SELECT count(*) FROM posts").fetchone()[0] == 0  # a probe, not a collection
    assert not social.sheet_path(SLUG).exists()


def test_paid_probe_needs_confirm_and_a_missing_key_skips(world, monkeypatch):
    started = len(fake.FakeApify.started)
    res = social.source_test(SLUG, config.load(SLUG), store.open_state(SLUG), confirm=True)
    by = {r["source"]: r for r in res["results"]}
    assert by["linkedin"]["status"] == "ok" and by["linkedin"]["items"] == 2
    assert by["linkedin"]["resolvable"] == 1  # Sam Carter at Brandly
    kinds = [k for k, _ in fake.FakeApify.started[started:]]
    assert kinds.count("posts") == 1 and "tweets" in kinds and "instagram" in kinds

    def no_key():
        raise apify.ApifyUnavailable("Apify key missing")
    monkeypatch.setattr(apify, "client", no_key)
    res = social.source_test(SLUG, config.load(SLUG), store.open_state(SLUG), confirm=True)
    by = {r["source"]: r for r in res["results"]}
    assert all(by[s]["status"] == "skipped" and by[s]["recommend"] == "unknown" for s in social.PAID)
    assert any("skipped" in n for n in res["notes"])
    assert by["reddit"]["recommend"] == "on"  # the free sources still ran

    out = CliRunner().invoke(app, ["signals", "test", "--product", SLUG])
    assert out.exit_code == 0, out.output
    assert "not run (add --confirm)" in out.output and "Reddit" in out.output and "saved:" in out.output


def test_sheet_rows_older_than_90_days_are_dropped_but_the_file_stays(world):
    from gtm.report import prune_old_files
    old = social.sheet_row(source="reddit", post_url="https://www.reddit.com/r/x/comments/old/", author_profile_url=None,
                           intent_code="ASKS_FOR_SOLUTION", summary="old need", company=None,
                           date=(now() - timedelta(days=91)).date().isoformat())
    new = social.sheet_row(source="reddit", post_url="https://www.reddit.com/r/x/comments/new/", author_profile_url=None,
                           intent_code="ASKS_FOR_SOLUTION", summary="new need", company=None)
    social.add_rows(SLUG, [old, new])
    assert prune_old_files(SLUG) >= 1
    assert social.sheet_path(SLUG).exists()
    assert [r["post_url"] for r in social.read_sheet(SLUG)] == [new["post_url"]]


def test_removed_rows_never_come_back(world):
    row = social.sheet_row(source="hackernews", post_url="https://news.ycombinator.com/item?id=1",
                           author_profile_url="https://news.ycombinator.com/user?id=someone",
                           intent_code="ASKS_FOR_SOLUTION", summary="x", company=None)
    social.add_rows(SLUG, [row])
    out = CliRunner().invoke(app, ["sheet", "--product", SLUG, "--remove", "https://news.ycombinator.com/user?id=someone"])
    assert out.exit_code == 0 and "removed 1 row(s)" in out.output
    assert social.add_rows(SLUG, [dict(row)]) == 0 and social.read_sheet(SLUG) == []


def test_sheet_command_prints_path_and_new_rows(world):
    social.add_rows(SLUG, [social.sheet_row(source="reddit", post_url=REDDIT_ANON, author_profile_url=None,
                                            intent_code="ASKS_FOR_SOLUTION", summary="wants a tool", company="Brandly")])
    out = CliRunner().invoke(app, ["sheet", "--product", SLUG, "--since", "3"])
    assert out.exit_code == 0, out.output
    assert str(social.sheet_path(SLUG)) in out.output and "1 row(s)" in out.output and REDDIT_ANON in out.output


def test_community_labelling_stays_within_two_calls(world, tmp_path, monkeypatch):
    conn = store.open_state(SLUG)
    for i in range(100):
        signals.store_post(conn, source="reddit", url=f"https://www.reddit.com/r/x/comments/{i}/", text="recommend a tool",
                           posted_at=None, author_name=None, author_handle=None, author_title=None,
                           company_page=False, keywords=["k"], run_id="t-run")
    ctx = _ctx(conn, tmp_path)
    signals.label(ctx)
    assert ctx.llm.calls == signals.COMMUNITY_LABEL_CALLS and ctx.llm.cap == 60
    assert conn.execute("SELECT count(*) FROM posts WHERE labelled_at IS NOT NULL").fetchone()[0] == 30

    # invalid answers are retried, but the retries still can't take the pass past its 2 calls
    monkeypatch.setattr(fake, "llm_response", lambda pid, payload, blocks: {"labels": "garbage"})
    ctx = _ctx(conn, tmp_path)
    signals.label(ctx)
    assert ctx.llm.calls == signals.COMMUNITY_LABEL_CALLS and ctx.llm.cap == 60
    assert any("label calls" in n for n in ctx.notes)


def test_fallback_search_junk_is_dropped():
    res = {"results": [
        {"url": "https://www.quora.com/?share=1", "title": "Quora", "snippet": "cart recovery"},  # home page
        {"url": "https://q.quora.com/_/ad/pixel", "title": "q.quora.com", "snippet": ""},
        {"url": "https://www.quora.com/Best-shoes", "title": "Best shoes", "snippet": "cart shoes"},  # 1 of 2 words
        {"url": "https://evil.example/cart-recovery", "title": "cart recovery", "snippet": ""},  # off-domain
        {"url": "https://www.quora.com/old", "title": "cart recovery tool?", "snippet": "",
         "freshness_signal": {"published_date": "2019-01-01"}},  # stale
        {"url": "https://www.quora.com/What-is-cart-recovery", "title": "Need a cart recovery tool", "snippet": ""}]}
    assert [i["url"] for i in social.fallback_items("quora", res, ["cart recovery"])] == \
        ["https://www.quora.com/What-is-cart-recovery"]
    forum = {"results": [{"url": "https://www.reddit.com/r/x/comments/1/", "title": "cart recovery", "snippet": ""},
                         {"url": "https://community.shop.example/t/1", "title": "cart recovery app?", "snippet": ""}]}
    assert [i["url"] for i in social.fallback_items("forums", forum, ["cart recovery"])] == \
        ["https://community.shop.example/t/1"]  # reddit has its own switch


def test_last30days_author_facts():
    """Shape of the pinned engine's raw export: Reddit items carry no author, HN items do."""
    export = {"items_by_source": {
        "reddit": [{"url": "https://www.reddit.com/r/a/comments/1/x/", "title": "T", "body": "T\nmine c1 c2",
                    "metadata": {"top_comments": [{"excerpt": "c1"}, {"excerpt": "c2"}]}}],
        "hackernews": [{"url": "https://blog.example/post", "title": "Ask HN", "body": "Ask HN", "author": "pg",
                        "metadata": {"hn_url": "https://news.ycombinator.com/item?id=9"}}]}}
    items = {i["source"]: i for i in last30days.items(export)}
    assert items["reddit"]["author"] is None and items["reddit"]["own_text"] == "T\nmine"
    assert items["hackernews"]["author"] == "pg"
    found = {i["source"]: i for i in social.community_items(list(items.values()))}
    assert found["reddit"]["author_profile"] is None
    assert found["hackernews"]["url"] == "https://news.ycombinator.com/item?id=9"
    assert found["hackernews"]["author_profile"] == "https://news.ycombinator.com/user?id=pg"
    assert social.profile_url("reddit", "[deleted]") is None and social.profile_url("reddit", "a/../b") is None


# ---------------------------------------------------------------- Reddit author lookup (paid, opt-in)

def _reddit_lookup(enabled: bool, max_per_run: int = 20) -> None:
    cfg = config.load(SLUG)
    cfg.signals.reddit_authors.enabled, cfg.signals.reddit_authors.max_per_run = enabled, max_per_run
    config.save(cfg)


def test_reddit_author_lookup_is_off_by_default_and_costs_nothing(world):
    fake.FakeApify.started.clear()
    _full_run()
    assert social.read_sheet(SLUG) and not [k for k, _ in fake.FakeApify.started if k == "reddit"]
    row = {r["post_url"]: r for r in social.read_sheet(SLUG)}[REDDIT_NAMED_CO]
    assert row["author_profile_url"] == ""


def test_reddit_author_lookup_fills_the_profile_once(world):
    _reddit_lookup(True)
    fake.FakeApify.started.clear()
    res = _full_run()
    rows = {r["post_url"]: r for r in social.read_sheet(SLUG)}
    assert rows[REDDIT_NAMED_CO]["author_profile_url"] == "https://www.reddit.com/user/brandly_ops"
    assert rows[REDDIT_ANON]["author_profile_url"] == "https://www.reddit.com/user/cartwoes_22"  # untouched
    runs = [inp for k, inp in fake.FakeApify.started if k == "reddit"]
    assert len(runs) == 1 and runs[0]["startUrls"] == [{"url": REDDIT_NAMED_CO}]  # only the row missing it
    assert runs[0]["maxPostCount"] == 1 and runs[0]["skipComments"] is True
    assert res["counts"]["reddit_authors_found"] == 1
    assert social.reddit_rows_missing_author(SLUG) == []
    _full_run()
    assert len([k for k, _ in fake.FakeApify.started if k == "reddit"]) == 1  # never paid twice
    conn = store.open_state(SLUG)
    assert not conn.execute("SELECT 1 FROM leads WHERE full_name LIKE '%brandly_ops%' "
                            "OR linkedin_url LIKE '%reddit%'").fetchone()  # still never a lead


def test_reddit_author_lookup_respects_erasure_and_unknown_authors(world, monkeypatch):
    _reddit_lookup(True)
    social.remove(SLUG, "https://www.reddit.com/user/brandly_ops")  # erased before it was ever found
    _full_run()
    row = {r["post_url"]: r for r in social.read_sheet(SLUG)}[REDDIT_NAMED_CO]
    assert row["author_profile_url"] == ""
    monkeypatch.setattr(fake, "REDDIT_AUTHORS", {"def456": "[deleted]"})
    assert social.profile_url("reddit", "[deleted]") is None


def test_reddit_author_lookup_without_key_skips_with_a_note(world, monkeypatch):
    _reddit_lookup(True)
    def no_key():
        raise apify.ApifyUnavailable("Apify key missing")
    monkeypatch.setattr(apify, "client", no_key)
    res = _full_run()
    assert any("Reddit author lookup skipped" in n for n in res["notes"]), res["notes"]
    assert social.reddit_rows_missing_author(SLUG)  # not marked tried: retried once a key exists


def test_sheet_command_shows_the_lookup_estimate(world):
    _full_run()
    out = CliRunner().invoke(app, ["sheet", "--product", SLUG]).output
    assert "Reddit rows without a profile: 1" in out and "OFF" in out and "$0.01" in out


def test_web_search_posts_need_a_recent_page_date(world, monkeypatch):
    """Live 2026-10-26: 6-7 year old Quora questions reached the intent sheet because web-search
    results carry no date and undated meant 'fresh'. Now the page's own date decides."""
    monkeypatch.setitem(fake.PAGE_DATES, "https://www.quora.com/What-is-a-good-cart-recovery-tool", "2019-05-01")
    items = [{"url": "https://www.quora.com/What-is-a-good-cart-recovery-tool", "posted_at": None},
             {"url": "https://www.quora.com/undated-question", "posted_at": None},
             {"url": "https://www.indiehackers.com/post/carts-xyz", "posted_at": None}]
    kept = social.dated_fresh(items)
    assert [i["url"] for i in kept] == ["https://www.indiehackers.com/post/carts-xyz"]
    assert kept[0]["posted_at"]
    assert not social._fresh_known(None) and not social._fresh_known("not a date")
