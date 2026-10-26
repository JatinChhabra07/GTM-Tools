"""Phase D1 (plan §15.2): the AI panel shortlists accounts BEFORE anything is paid for."""

from __future__ import annotations

import json
from types import SimpleNamespace
from datetime import timedelta

import pytest

from gtm import config, fake, run, store
from gtm.util import now

from tests.test_pipeline import SLUG, world  # noqa: F401  (fixture)


def _rows(conn, run_id):
    return {r["domain"]: r for r in conn.execute("SELECT * FROM shortlist WHERE run_id = ?", (run_id,))}


def _enriched_domains(conn):
    return {r[0] for r in conn.execute("SELECT account_domain FROM leads WHERE enriched_at IS NOT NULL")}


def _patch_llm(monkeypatch, fn):
    real = fake.llm_response
    monkeypatch.setattr(fake, "llm_response", lambda pid, payload, blocks: fn(pid, payload, blocks, real))


def test_only_shortlisted_accounts_are_enriched(world):
    res = run.execute(SLUG, "full", limit=1)
    assert res["status"] == "done", res
    conn = store.open_state(SLUG)
    rows = _rows(conn, res["run_id"])
    kept = {d for d, r in rows.items() if r["verdict"] == "keep"}
    assert len(kept) == 1 and len(rows) >= 2, rows  # pool bigger than today's quota
    assert all(r["verdict"] == "not_picked" for d, r in rows.items() if d not in kept)
    assert _enriched_domains(conn) <= kept
    assert "clientco.example" not in rows  # clients never enter the pool


def test_panel_drop_blocks_enrichment_and_cools_down(world, monkeypatch):
    def drop_brandly(pid, payload, blocks, real):
        out = real(pid, payload, blocks)
        if pid == "shortlist_synth":
            for v in out["verdicts"]:
                if v["domain"] == "brandly.example":
                    v.update(verdict="drop", reason="namesake signal")
        return out
    _patch_llm(monkeypatch, drop_brandly)
    res = run.execute(SLUG, "full", limit=5)
    conn = store.open_state(SLUG)
    assert _rows(conn, res["run_id"])["brandly.example"]["verdict"] == "drop"
    assert "brandly.example" not in _enriched_domains(conn)

    res2 = run.execute(SLUG, "refresh", limit=5)
    assert "brandly.example" not in _rows(conn, res2["run_id"])  # cooling down: not even reviewed

    conn.execute("INSERT INTO observations (account_domain, collector, signal_code, value_text, confidence, "
                 "evidence_url, observed_at) VALUES ('brandly.example', 'test', 'FUNDING', 'Series A', 0.9, "
                 "'https://news.example/brandly-series-a', ?)",
                 ((now() + timedelta(seconds=2)).isoformat(timespec="seconds"),))
    from gtm import shortlist

    c = SimpleNamespace(conn=conn, cfg=config.load(SLUG))  # the cooldown only needs conn + cfg
    assert not shortlist.cooling_down(c, "brandly.example")  # a newer signal reopens it


def test_panel_failure_pays_for_nothing(world, monkeypatch):
    def broken(pid, payload, blocks, real):
        return {"nonsense": True} if pid.startswith("shortlist_") else real(pid, payload, blocks)
    _patch_llm(monkeypatch, broken)
    fake.FakeApify.started.clear()
    res = run.execute(SLUG, "full", limit=5)
    conn = store.open_state(SLUG)
    assert not _enriched_domains(conn)
    assert not [k for k, _ in fake.FakeApify.started if k in ("employees", "people")]
    assert any("nothing paid" in n for n in res["notes"]), res["notes"]
    assert all(r["verdict"] == "not_picked" for r in _rows(conn, res["run_id"]).values())


def test_resume_reuses_the_shortlist(world, monkeypatch):
    from gtm import enrich
    real_run = enrich.run
    calls = {"n": 0}

    def flaky(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("apify outage")
        return real_run(ctx)
    monkeypatch.setattr(enrich, "run", flaky)
    first = run.execute(SLUG, "full", limit=5)
    assert first["status"] == "failed"
    second = run.execute(SLUG, "full", limit=5)
    assert second["resumed"] and second["run_id"] == first["run_id"] and second["status"] == "done", second
    conn = store.open_state(SLUG)
    n_panel = conn.execute("SELECT count(*) FROM usage_events WHERE prompt_id = 'shortlist_worker'").fetchone()[0]
    assert n_panel == 1  # the finished shortlist was not re-run (no second spend)
    kept = {d for d, r in _rows(conn, first["run_id"]).items() if r["verdict"] == "keep"}
    assert kept and _enriched_domains(conn) <= kept


@pytest.mark.parametrize("profile,reviewers", [("max5x", 2), ("max20x", 3)])
def test_reviewer_count_follows_the_plan(world, monkeypatch, profile, reviewers):
    cfg = config.load(SLUG)
    cfg.usage.profile = profile
    config.save(cfg)
    run.execute(SLUG, "full", limit=5)
    conn = store.open_state(SLUG)
    n = conn.execute("SELECT count(*) FROM usage_events WHERE prompt_id = 'shortlist_review'").fetchone()[0]
    assert n == reviewers


def test_fact_sheets_reach_the_panel_only_as_untrusted_blocks(world, monkeypatch):
    seen = {}

    def spy(pid, payload, blocks, real):
        if pid == "shortlist_worker":
            seen["payload"], seen["blocks"] = payload, blocks
        return real(pid, payload, blocks)
    _patch_llm(monkeypatch, spy)
    run.execute(SLUG, "full", limit=5)
    assert seen["blocks"] and all(k.startswith("account:") for k in seen["blocks"])
    assert "http" not in json.dumps(seen["payload"]["domains"])  # trusted input: bare domains only


def test_wigolo_registration_never_clobbers(tmp_path):
    from gtm.sources import wigolo
    mcp = tmp_path / ".mcp.json"
    assert wigolo.register_project(mcp, already_listed=True) == "already available" and not mcp.exists()
    mcp.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    assert "added" in wigolo.register_project(mcp, already_listed=None)
    data = json.loads(mcp.read_text())["mcpServers"]
    assert data["other"] == {"command": "x"} and data["wigolo"] == {"command": "npx", "args": ["-y", "wigolo"]}
    data["wigolo"]["env"] = {"WIGOLO_SEARCH": "hybrid"}
    mcp.write_text(json.dumps({"mcpServers": data}))
    assert wigolo.register_project(mcp, already_listed=False) == "already in .mcp.json"
    assert json.loads(mcp.read_text())["mcpServers"]["wigolo"]["env"] == {"WIGOLO_SEARCH": "hybrid"}


def test_reviewer_domains_match_loosely_and_unreviewed_accounts_are_not_dropped(world, monkeypatch):
    """Live VizitorApp run: findings echoed as 'www.x' were lost, and the synth then dropped
    unreviewed companies 'in doubt', putting them on a 14-day cooldown."""
    def skew(pid, payload, blocks, real):
        out = real(pid, payload, blocks)
        if pid == "shortlist_review":
            out["findings"] = [dict(f, domain="https://www." + f["domain"] + "/") for f in out["findings"]
                               if f["domain"] != "shopco.example"]  # every lens skips ShopCo
        if pid == "shortlist_synth":
            for v in out["verdicts"]:
                v.update(verdict="drop" if v["domain"] == "shopco.example" else "keep", reason="checked")
        return out
    _patch_llm(monkeypatch, skew)
    res = run.execute(SLUG, "full", limit=5)
    conn = store.open_state(SLUG)
    rows = _rows(conn, res["run_id"])
    assert rows["brandly.example"]["verdict"] == "keep" and rows["brandly.example"]["reason"] == "checked"
    assert any(f["from"] != "worker" for f in json.loads(rows["brandly.example"]["findings_json"]))
    assert rows["shopco.example"]["verdict"] == "not_picked", dict(rows["shopco.example"])


def test_panel_reviews_in_rounds_and_stops_when_enough_passed(world, monkeypatch):
    from gtm import shortlist
    monkeypatch.setattr(shortlist, "PANEL_BATCH", 1)
    seen = []

    def spy(pid, payload, blocks, real):
        if pid == "shortlist_worker":
            seen.append(tuple(payload["domains"]))
        return real(pid, payload, blocks)
    _patch_llm(monkeypatch, spy)
    res = run.execute(SLUG, "full", limit=1)
    conn = store.open_state(SLUG)
    rows = _rows(conn, res["run_id"])
    assert all(len(d) == 1 for d in seen)  # one company per round
    assert len(seen) == 1  # the first company passed and filled the quota: no paid-for extra rounds
    assert sum(r["verdict"] == "keep" for r in rows.values()) == 1

