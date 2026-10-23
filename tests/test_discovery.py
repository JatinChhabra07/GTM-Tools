"""Phase D4 (plan §15): engine attribution, lookalikes for startups, the per-engine funnel."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from gtm import accounts, config, discovery, feedback, ledger, report, store
from gtm.cli import app
from gtm.paths import product_config_dir
from gtm.util import now_iso

from tests.test_pipeline import SLUG, full_run, world  # noqa: F401  (fixture)

runner = CliRunner()


def _hits(conn, engine=None):
    rows = conn.execute("SELECT * FROM discovery_hits" + (" WHERE engine = ?" if engine else ""),
                        (engine,) if engine else ()).fetchall()
    return {(r["domain"], r["engine"]): r for r in rows}


def _build(*extra):
    r = runner.invoke(app, ["accounts", "build", "--product", SLUG, "--estimate", *extra])
    assert r.exit_code == 0, r.output
    return json.loads(r.output)


def _no_clients(conn):
    conn.execute("DELETE FROM clients")
    conn.execute("UPDATE accounts SET status = 'new' WHERE status = 'client'")
    (product_config_dir(SLUG) / "clients.csv").unlink()


# ---------------------------------------------------------------- attribution

def test_icp_and_lookalike_queries_are_attributed_separately(world):
    out = _build()
    assert out["queries"]["icp"] > 0 and out["queries"]["lookalike"] == 2  # "companies like ClientCo", "... competitors"
    conn = store.open_state(SLUG)
    hits = _hits(conn)
    # the fake search returns every company for both kinds of query: one row per engine, never merged
    for d in ("shopco.example", "brandly.example"):
        icp, look = hits[(d, "icp")], hits[(d, "lookalike")]
        assert icp["evidence_url"].startswith("https://") and look["evidence_url"].startswith("https://")
        assert "companies" in icp["detail"] and "ClientCo" in look["detail"] and "ClientCo" not in icp["detail"]
    assert out["engine_hits"] >= 4
    _build()  # found again: one row per (domain, engine), hits bumped
    again = _hits(conn)
    assert len(again) == len(hits) and again[("shopco.example", "icp")]["hits"] == 2


def test_list_mined_company_inherits_the_engine_of_its_query(world, monkeypatch):
    from gtm import llm
    from gtm.sources import wigolo
    lst = "https://www.listsite.example/lists/top-d2c"
    monkeypatch.setattr(wigolo, "search", lambda q, **kw: {"results": [
        {"url": lst, "title": "Top D2C brands", "snippet": "list"}] if any("like" in x for x in q) else []})
    conn = store.open_state(SLUG)
    found = accounts.candidates_from_search(llm.Session(conn, run_id="t", cap=6), config.load(SLUG),
                                            [("icp", "d2c brands US"), ("lookalike", "companies like ClientCo")],
                                            competitors=[])
    brandly = next(c for c in found if c["domain"] == "brandly.example")
    assert [h["engine"] for h in brandly["hits"]] == ["lookalike"]
    assert brandly["hits"][0]["evidence"] == lst and "companies like ClientCo" in brandly["hits"][0]["detail"]
    accounts.ensure_account(conn, domain="brandly.example", name="Brandly", source="tal", evidence=lst)
    assert accounts.record_hits(conn, brandly) == 1
    assert set(discovery.engines_for(conn, "brandly.example")) == {"lookalike"}


def test_engines_take_turns_mining_lists(world, monkeypatch):
    """Live searches return mostly lists; the ICP queries run first and must not use up every slot."""
    from gtm import llm
    from gtm.sources import wigolo
    monkeypatch.setattr(accounts, "MAX_LISTS", 2)

    def search(q, **kw):
        kind = "look" if any("like" in x for x in q) else "icp"
        n = 1 if kind == "look" else 3
        return {"results": [{"url": f"https://{kind}{i}.example/lists/top", "title": "Top", "snippet": "list"}
                            for i in range(n)]}
    monkeypatch.setattr(wigolo, "search", search)
    conn = store.open_state(SLUG)
    session = llm.Session(conn, run_id="t", cap=10)
    found = accounts.candidates_from_search(session, config.load(SLUG),
                                            [("icp", "d2c brands"), ("lookalike", "companies like ClientCo")],
                                            competitors=[])
    engines = {h["engine"] for c in found for h in c["hits"]}
    assert engines == {"icp", "lookalike"} and session.calls == 4  # 2 searches + 2 lists (one per engine)


def test_list_tracking_links_give_real_domains(world, monkeypatch):
    """Seen live: encoded redirect links read as '2fwww.brand.com', affiliate hosts as companies."""
    from gtm.sources import wigolo
    md = ("[Cariuma](https://click.example/?u=https%3A%2F%2Fwww.cariuma.com%2Fshoes) "
          "[Rothys](https://rothys.sjv.io/abc) [Deal](https://www.shareasale.com/r.cfm?u=1)")
    monkeypatch.setattr(wigolo, "fetch", lambda url, **kw: {"markdown": md, "links": []})
    domains = {e["url"] for e in accounts.list_entries("https://list.example/top", "Top shoes")}
    assert "https://cariuma.com/" in domains
    assert not any("2f" in d or "sjv.io" in d or "shareasale" in d for d in domains)


def test_bare_string_queries_still_count_as_icp(world, monkeypatch):
    from gtm import llm
    conn = store.open_state(SLUG)
    found = accounts.candidates_from_search(llm.Session(conn, run_id="t", cap=6), config.load(SLUG),
                                            ["d2c brands"], competitors=[])
    assert found and all([h["engine"] for h in c["hits"]] == ["icp"] for c in found)


def test_social_engine_records_resolved_posts_without_personal_urls(world):
    full_run()
    conn = store.open_state(SLUG)
    social = _hits(conn, "social")
    row = social[("brandly.example", "social")]  # Sam Carter's post named Brandly as his employer
    assert row["evidence_url"].startswith("https://") and "linkedin.com/posts" not in row["evidence_url"]
    # found by Sam Carter's LinkedIn post and by the Reddit post naming Brandly (D3): either is a social find
    assert row["detail"].split(" ")[0] in ("linkedin", "reddit") and "ASKS_FOR_SOLUTION" in row["detail"]
    assert row["hits"] >= 2
    assert ("shopco.example", "social") in social  # its LinkedIn job posts
    assert social[("shopco.example", "social")]["evidence_url"].startswith("https://www.linkedin.com/jobs/")
    rep = (store.open_state(SLUG).execute("SELECT run_id FROM runs").fetchone()[0])
    from gtm.paths import runs_dir
    text = (runs_dir(SLUG) / rep / "report.md").read_text()
    assert "## Discovery engines" in text and "| social |" in text and "counts for each" in text


def test_manual_import_records_new_and_known_accounts(world, tmp_path):
    accounts.ensure_account(store.open_state(SLUG), domain="shopco.example", name="ShopCo", source="tal",
                            evidence="https://shopco.example/")
    csv = tmp_path / "mine.csv"
    csv.write_text("company,domain\nShopCo,https://www.shopco.example/\nBrandly,brandly.example\n,\n")
    r = runner.invoke(app, ["accounts", "import", "--product", SLUG, "--file", str(csv)])
    assert r.exit_code == 0, r.output
    manual = _hits(store.open_state(SLUG), "manual")
    assert set(d for d, _ in manual) == {"shopco.example", "brandly.example"}
    assert all(r["detail"] == "imported from mine.csv" for r in manual.values())


def test_record_validates(world):
    conn = store.open_state(SLUG)
    assert not discovery.record(conn, "x.example", "icp", evidence_url="javascript:alert(1)")
    assert not discovery.record(conn, "", "icp", evidence_url="https://x.example/")
    with pytest.raises(ValueError):
        discovery.record(conn, "x.example", "telepathy", evidence_url="https://x.example/")
    with pytest.raises(Exception):  # the table refuses it too, whoever writes
        conn.execute("INSERT INTO discovery_hits VALUES ('x.example','icp',NULL,'ftp://x',?,?,1)", (now_iso(), now_iso()))


# ---------------------------------------------------------------- backfill

def test_backfill_is_idempotent_and_runs_on_upgrade(world):
    conn = store.open_state(SLUG)
    accounts.ensure_account(conn, domain="sig.example", name="Sig", source="signal", evidence="https://jobs.example/1")
    accounts.ensure_account(conn, domain="tal.example", name="Tal", source="tal", evidence="https://tal.example/")
    accounts.ensure_account(conn, domain="near.example", name="Near", source="tal", evidence="https://near.example/",
                            status="near_miss")
    conn.execute("DELETE FROM discovery_hits")
    first = discovery.backfill(conn)
    hits = _hits(conn)
    assert hits[("sig.example", "social")]["evidence_url"] == "https://jobs.example/1"
    assert hits[("tal.example", "icp")]["detail"] == "backfill"
    assert not any(d in ("clientco.example", "near.example") for d, _ in hits)  # never "discovered"
    assert first == len(hits) and discovery.backfill(conn) == 0 and len(_hits(conn)) == len(hits)
    # a real find replaces the backfill placeholder
    discovery.record(conn, "tal.example", "icp", evidence_url="https://list.example/top", detail="search: x")
    row = _hits(conn)[("tal.example", "icp")]
    assert row["detail"] == "search: x" and row["hits"] == 2

    # an existing database upgraded to this schema is attributed once, by open_state
    conn.execute("DELETE FROM discovery_hits")
    conn.execute("DROP TABLE discovery_hits")
    conn.execute(f"PRAGMA user_version = {store._DISCOVERY_VERSION - 1}")
    conn.close()
    assert ("sig.example", "social") in _hits(store.open_state(SLUG))


# ---------------------------------------------------------------- lookalikes for startups

def test_no_clients_skips_lookalike_with_a_note(world):
    _no_clients(store.open_state(SLUG))
    out = _build()
    assert out["queries"]["lookalike"] == 0 and "lookalike search skipped" in out["notes"][0]
    assert not _hits(store.open_state(SLUG), "lookalike")
    ef = report.engine_funnel(store.open_state(SLUG), ledger.open_ledger(), SLUG)
    assert "no clients yet" in ef["engines"]["lookalike"]["note"]


def test_no_icp_and_no_seeds_is_a_plain_failure(world):
    _no_clients(store.open_state(SLUG))
    cfg = config.load(SLUG)
    cfg.icp.segments = []
    config.save(cfg)
    r = runner.invoke(app, ["accounts", "build", "--product", SLUG, "--estimate"])
    assert r.exit_code == 1 and "dream customers" in r.output


def test_dream_customers_seed_queries_but_not_the_lookalike_weight(world):
    from gtm.score import client_prints, weights
    conn = store.open_state(SLUG)
    _no_clients(conn)
    accounts.ensure_account(conn, domain="shopco.example", name="ShopCo", source="tal", evidence="https://shopco.example/")
    accounts.profile_accounts(conn, config.load(SLUG), ["shopco.example"])
    accounts.rescore_accounts(conn, config.load(SLUG))
    before = dict(conn.execute("SELECT prescore, lookalike_score FROM accounts WHERE domain = 'shopco.example'").fetchone())

    (product_config_dir(SLUG) / "dream_customers.csv").write_text(
        "company,domain\nBrandly,https://www.brandly.example/\nRivalCart,rivalcart.example\nJunk,linkedin.com\n")
    r = runner.invoke(app, ["accounts", "clients", "--product", SLUG])
    assert r.exit_code == 0, r.output
    res = json.loads(r.output)
    assert res["dream_customers"] == 2 and "dream customers as seeds" in res["note"]
    assert set(accounts.lookalike_queries(conn, SLUG)) == {"companies like Brandly", "Brandly competitors",
                                                           "companies like RivalCart", "RivalCart competitors"}
    manual = _hits(conn, "manual")
    assert set(manual) == {("brandly.example", "manual"), ("rivalcart.example", "manual")}
    assert conn.execute("SELECT status FROM accounts WHERE domain = 'brandly.example'").fetchone()[0] != "client"
    # they are targets and seeds, never proof: no client fingerprint, the lookalike weight stays at "no clients"
    assert client_prints(conn) == [] and weights(config.load(SLUG), 0) == weights(config.load(SLUG), len(client_prints(conn)))
    accounts.rescore_accounts(conn, config.load(SLUG))
    after = dict(conn.execute("SELECT prescore, lookalike_score FROM accounts WHERE domain = 'shopco.example'").fetchone())
    assert after == before

    out = _build()
    assert out["queries"]["lookalike"] == 4 and "notes" not in out
    look = _hits(conn, "lookalike")
    assert look and all("Brandly" in r["detail"] or "RivalCart" in r["detail"] for r in look.values())


def test_clients_come_before_dream_customers_as_seeds(world):
    conn = store.open_state(SLUG)
    (product_config_dir(SLUG) / "dream_customers.csv").write_text("company,domain\nBrandly,brandly.example\n")
    assert accounts.lookalike_seeds(conn, SLUG) == ["ClientCo", "Brandly"]
    assert accounts.lookalike_seeds(conn) == ["ClientCo"]


# ---------------------------------------------------------------- per-engine funnel

def _lead(conn, key, domain, *, enriched=True, qualified=False, pushed=False, reply=None, bounced=False):
    conn.execute("INSERT INTO leads (lead_key, account_domain, full_name, status, enriched_at) VALUES (?,?,?,?,?)",
                 (key, domain, key, "enriched", now_iso() if enriched else None))
    if qualified:
        conn.execute("INSERT INTO scores (lead_key, run_id, total, dims_json, gates_json, verdict, feature_snapshot_json, "
                     "created_at) VALUES (?, 'r1', 80, '{}', '{}', 'keep', '{}', ?)", (key, now_iso()))
    if pushed:
        conn.execute("INSERT INTO sequences (seq_id, lead_key, steps_json, reference_policy, recipient_email, mailbox_id, "
                     "content_hash, status, updated_at, pushed_at) VALUES (?,?,'[]','theme_only',?,'mb_1','h','pushed',?,?)",
                     (f"s-{key}", key, f"{key}@{domain}", now_iso(), now_iso()))
        if bounced:
            ledger.open_ledger().execute(
                "INSERT INTO account_sequences (id, product, lead_key, email_hash, domain, mailbox_id, status, started_at) "
                "VALUES (?,?,?,?,?,'mb_1','bounced',?)", (f"s-{key}", SLUG, key, "x", domain, now_iso()))
    if reply:
        conn.execute("INSERT INTO replies (id, seq_id, received_at, body_stripped, intent, status, lead_key) "
                     "VALUES (?,?,?,'',?,'handled',?)", (f"r-{key}", f"s-{key}", now_iso(), reply, key))


def test_engine_funnel_matches_a_hand_built_scenario(world):
    conn = store.open_state(SLUG)
    conn.execute("DELETE FROM discovery_hits")
    for d, src in (("a.example", "tal"), ("b.example", "tal"), ("c.example", "signal"), ("d.example", "tal")):
        accounts.ensure_account(conn, domain=d, name=d, source=src, evidence=f"https://{d}/")
    rec = lambda d, e: discovery.record(conn, d, e, evidence_url=f"https://{d}/", detail="t")  # noqa: E731
    rec("a.example", "icp")
    rec("b.example", "icp")
    rec("b.example", "social")  # found twice: B and its lead count for both engines
    rec("c.example", "social")
    rec("d.example", "lookalike")
    rec("clientco.example", "lookalike")  # a client: never counted as found
    for d in ("a.example", "b.example"):
        conn.execute("INSERT INTO shortlist (domain, run_id, verdict, at) VALUES (?, 'r1', 'keep', ?)", (d, now_iso()))
    _lead(conn, "a1", "a.example", qualified=True, pushed=True, reply="interested")
    _lead(conn, "b1", "b.example", qualified=True, pushed=True, reply="ooo")  # an auto-reply is not a reply
    _lead(conn, "c1", "c.example")
    _lead(conn, "c2", "c.example", qualified=True, pushed=True, bounced=True)
    _lead(conn, "c3", "c.example", enriched=False)

    ef = report.engine_funnel(conn, ledger.open_ledger(), SLUG)
    e = ef["engines"]
    pick = lambda r: {k: r[k] for k in ("found", "shortlisted", "enriched", "qualified", "contacted", "delivered",  # noqa: E731
                                        "replied", "positive")}
    assert pick(e["icp"]) == dict(found=2, shortlisted=2, enriched=2, qualified=2, contacted=2, delivered=2,
                                  replied=1, positive=1)
    assert pick(e["social"]) == dict(found=2, shortlisted=1, enriched=3, qualified=2, contacted=2, delivered=1,
                                     replied=0, positive=0)
    assert pick(e["lookalike"]) == dict(found=1, shortlisted=0, enriched=0, qualified=0, contacted=0, delivered=0,
                                        replied=0, positive=0)
    assert e["competitor"]["found"] == 0 and e["manual"]["found"] == 0
    lo, hi = report.wilson(1, 2)
    assert e["icp"]["positive_per_100"] == 50.0 and e["icp"]["positive_ci95_per_100"] == [round(100 * lo, 1), round(100 * hi, 1)]
    assert e["icp"]["replies_per_100"] == 50.0
    assert all(r["too_few_to_tell"] for r in e.values())
    assert ef["leader"] is None and "too few" in ef["leader_note"] and "counts for each" in ef["note"]

    # the window is a cohort by first find
    conn.execute("UPDATE discovery_hits SET first_seen_at = '2020-01-01T00:00:00+00:00' WHERE domain = 'a.example'")
    assert report.engine_funnel(conn, ledger.open_ledger(), SLUG, days=30)["engines"]["icp"]["found"] == 1

    r = runner.invoke(app, ["report", "--product", SLUG, "--engines", "--days", "30"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["engines"]["social"]["found"] == 2


def _row(delivered, pos):
    lo, hi = report.wilson(pos, delivered)
    return {"delivered": delivered, "positive": pos, "positive_per_100": round(100 * pos / delivered, 1),
            "positive_ci95_per_100": [round(100 * lo, 1), round(100 * hi, 1)],
            "too_few_to_tell": delivered < report.MIN_SAMPLE}


def test_no_winner_on_small_or_overlapping_samples():
    # a huge gap on a small sample is still no winner
    assert report._leader({"icp": _row(200, 2), "lookalike": _row(20, 10)})["leader"] is None
    # enough sends but overlapping ranges: no winner
    assert report._leader({"icp": _row(100, 6), "lookalike": _row(100, 9)})["leader"] is None
    # enough sends and clearly separated: a winner
    assert report._leader({"icp": _row(300, 3), "lookalike": _row(300, 45)})["leader"] == "lookalike"


def test_engine_proposal_is_only_a_suggestion(world, monkeypatch):
    conn = store.open_state(SLUG)
    fake_funnel = {"leader": "social", "engines": {"social": {**_row(150, 20), "replied": 25},
                                                   "icp": {**_row(150, 2), "replied": 5}}}
    monkeypatch.setattr(report, "engine_funnel", lambda *a, **k: fake_funnel)
    assert feedback._propose_engine(conn, ledger.open_ledger(), SLUG) == 1
    assert feedback._propose_engine(conn, ledger.open_ledger(), SLUG) == 0  # no duplicate
    row = conn.execute("SELECT * FROM proposals WHERE kind = 'engine'").fetchone()
    assert row["status"] == "open" and json.loads(row["body_json"])["engine"] == "social"
    fake_funnel["engines"]["social"] = _row(80, 20)  # below the evidence bar: no proposal
    conn.execute("DELETE FROM proposals")
    assert feedback._propose_engine(conn, ledger.open_ledger(), SLUG) == 0
