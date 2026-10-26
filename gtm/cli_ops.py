"""Operational commands: runs, review, sending, mailboxes, Snov wiring, accounts, schedule,
erasure. Registered on the main app in gtm/cli.py.

Founder-only commands (review, mailbox add, sending resume, autosend enable, forget) refuse to run
inside Claude Code or without a TTY, and the signing ones need the PIN.
"""

from __future__ import annotations

import getpass
import json
from datetime import timedelta
from pathlib import Path

import typer

from gtm import catalog, config, ledger, signing, store
from gtm.paths import runs_dir
from gtm.util import now, now_iso, parse_iso

run_app = typer.Typer(no_args_is_help=True, help="Run the pipeline")
mailbox_app = typer.Typer(no_args_is_help=True, help="Sending mailboxes (limits are PIN-signed)")
snov_app = typer.Typer(no_args_is_help=True, help="Connect Snov.io lists/campaigns")
sending_app = typer.Typer(no_args_is_help=True, help="Global kill switch")
autosend_app = typer.Typer(no_args_is_help=True, help="Auto-send (off by default; PIN to enable)")
accounts_app = typer.Typer(no_args_is_help=True, help="Clients, target accounts, pre-score")
signals_app = typer.Typer(no_args_is_help=True, help="The signal catalog: confirm what may be collected")
schedule_app = typer.Typer(no_args_is_help=True, help="Cron schedule")
manual_app = typer.Typer(no_args_is_help=True, help="Things the APIs can't do, waiting for you")


def register(app: typer.Typer) -> None:
    app.command("run")(run_cmd)
    app.command("review")(review_cmd)
    app.command("feedback")(feedback_cmd)
    app.command("report")(report_cmd)
    app.command("warmup")(warmup_cmd)
    app.command("forget")(forget_cmd)
    app.command("crawl")(crawl_cmd)
    app.command("sheet")(sheet_cmd)
    app.command("leads")(leads_cmd)
    for name, sub in (("mailbox", mailbox_app), ("snov", snov_app), ("sending", sending_app),
                      ("autosend", autosend_app), ("accounts", accounts_app), ("signals", signals_app),
                      ("schedule", schedule_app), ("manual", manual_app)):
        app.add_typer(sub, name=name)


def _fail(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _human(action: str) -> None:
    from gtm.cli import require_human_terminal
    require_human_terminal(action)


def _unlock():
    if not signing.keys_exist():
        _fail("no signing key yet: run `bin/gtm keys init` first")
    for _ in range(3):
        try:
            return signing.unlock(getpass.getpass("Approval PIN: "))
        except signing.WrongPin:
            typer.echo("wrong PIN")
    _fail("3 wrong PINs")


def crawl_cmd(product: str = typer.Option(...),
              url: str = typer.Option(None, help="default: the product's own website"),
              max_pages: int = typer.Option(25, min=1, max=60)) -> None:
    """Read the product website into products/<slug>/knowledge/pages/ (wigolo; Firecrawl fallback)."""
    from gtm import site_crawl
    cfg = config.load(product)
    _echo(site_crawl.crawl(product, url or str(cfg.product.url), max_pages=max_pages))


def _echo(obj) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


# ---------------------------------------------------------------- run / review / feedback / report

def run_cmd(product: str = typer.Option(...), mode: str = typer.Option("full", help="full|refresh|sync|noop"),
            limit: int = typer.Option(None, help="daily lead cap before a READY mailbox exists (default 10)")) -> None:
    """One pipeline run (cron calls this). Exits non-zero on failure."""
    from gtm import run
    try:
        res = run.execute(product, mode, limit=limit)
    except run.AlreadyRunning as e:
        _fail(str(e))
    _echo(res)
    if res["status"] == "failed":
        raise typer.Exit(1)


def review_cmd(product: str = typer.Option(...), drafts: bool = typer.Option(False, help="review reply drafts"),
               batch_min_score: float = typer.Option(None, help="approve high scorers after typing the count")) -> None:
    """Review and approve emails. YOUR terminal only; asks for your PIN once."""
    _human("gtm review")
    from gtm import review
    from gtm.run import product_lock
    cfg = config.load(product)
    key = _unlock()
    with product_lock(product):
        conn = store.open_state(product)
        if drafts:
            _echo(review.drafts_desk(conn, product, key))
        else:
            _echo(review.desk(cfg, conn, ledger.open_ledger(), product, key, batch_min_score=batch_min_score))


def feedback_cmd(product: str = typer.Option(...)) -> None:
    """Daily: classify replies, draft answers (reviewed in `gtm review --drafts`), learning proposals."""
    from gtm import feedback
    from gtm.run import product_lock
    with product_lock(product):
        _echo(feedback.run(product))


def report_cmd(product: str = typer.Option(...), run: str = typer.Option(None, help="a run id; default: latest"),
               outcomes: bool = typer.Option(False, help="reply-rate metrics with confidence intervals"),
               proposals: bool = typer.Option(False, help="open learning proposals from reply data"),
               close: int = typer.Option(None, help="with --proposals: mark this proposal id handled"),
               accepted: bool = typer.Option(True, help="with --close: accepted (default) or --no-accepted"),
               engines: bool = typer.Option(False, help="funnel per discovery engine (icp, lookalike, social, ...)"),
               days: int = typer.Option(None, min=1, help="with --engines: only companies found in the last N days")) -> None:
    """The latest run report (aggregates only, safe to show in a Claude session)."""
    from gtm import report
    conn = store.open_state(product)
    if engines:
        _echo(report.engine_funnel(conn, ledger.open_ledger(), product, days=days))
        return
    if proposals:
        if close is not None:
            conn.execute("UPDATE proposals SET status = ? WHERE id = ?", ("accepted" if accepted else "rejected", close))
        _echo([{"id": r["id"], "kind": r["kind"], "at": r["at"], **json.loads(r["body_json"])}
               for r in conn.execute("SELECT * FROM proposals WHERE status = 'open' ORDER BY at")])
        return
    if outcomes:
        _echo(report.outcomes(conn, ledger.open_ledger(), product))
        return
    row = conn.execute("SELECT run_id FROM runs WHERE run_id = ?" if run else
                       "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1", (run,) if run else ()).fetchone()
    if not row:
        _fail("no runs yet")
    path = runs_dir(product) / row[0] / "report.md"
    typer.echo(path.read_text() if path.exists() else f"run {row[0]} has no report")


# ---------------------------------------------------------------- warmup / mailboxes

def warmup_cmd(domain: str = typer.Argument(..., help="your SENDING domain, e.g. getacme.com"),
               selector: list[str] = typer.Option(None, help="DKIM selector(s) if you know them")) -> None:
    """Deliverability check: MX, SPF, DKIM, DMARC, domain age, blocklists. Saves READY/NOT READY."""
    from gtm import mailboxes, warmup
    rep = warmup.check_domain(domain, dkim_selectors=list(selector) if selector else None)
    updated = mailboxes.set_ready(ledger.open_ledger(), rep.domain, rep.verdict)
    _echo({"domain": rep.domain, "verdict": rep.verdict, "reasons": rep.reasons,
           "checks": [c.__dict__ for c in rep.checks], "mailboxes_updated": updated})


@mailbox_app.command("add")
def mailbox_add(id: str = typer.Option(..., help="short id, e.g. mb_1"), address: str = typer.Option(...),
                daily_cap: int = typer.Option(30), warmup_start: str = typer.Option(None, help="YYYY-MM-DD warm-up began"),
                warmup_min_days: int = typer.Option(14), start_sends: int = typer.Option(5),
                daily_increase: int = typer.Option(3), catchall_allowed: bool = typer.Option(False)) -> None:
    """Register a sending mailbox. YOUR terminal + PIN (its limits are signed)."""
    _human("gtm mailbox add")
    from gtm import mailboxes
    key = _unlock()
    mailboxes.add(ledger.open_ledger(), key, mailbox_id=id, address=address, daily_cap=daily_cap,
                  warmup_start=warmup_start, warmup_min_days=warmup_min_days, start_sends=start_sends,
                  daily_increase=daily_increase, catchall_allowed=catchall_allowed)
    typer.echo(f"mailbox {id} saved. Next: bin/gtm warmup {address.split('@')[-1]}")


@mailbox_app.command("list")
def mailbox_list() -> None:
    from gtm import mailboxes
    led = ledger.open_ledger()
    out = []
    for r in led.execute("SELECT * FROM mailboxes").fetchall():
        rate, b, s = mailboxes.bounce_rate(led, r["id"])
        out.append({"id": r["id"], "address": r["address"], "ready": r["ready_state"], "paused": bool(r["paused"]),
                    "signed": mailboxes.signed_ok(r), "cap_today": mailboxes.cap_on(r, now().date()),
                    "touches_today": mailboxes.touches_on(led, r["id"], now().date()),
                    "bounce_7d": f"{b}/{s}"})
    _echo(out)


# ---------------------------------------------------------------- Snov wiring

@snov_app.command("setup")
def snov_setup(product: str = typer.Option(...), mailbox: str = typer.Option(...)) -> None:
    """Create the do-not-email list (once) and this product+mailbox's prospect list; print the campaign checklist."""
    from gtm.sources import snov
    cfg = config.load(product)
    led = ledger.open_ledger()
    client = snov.client()
    dne = led.execute("SELECT value FROM settings WHERE key = 'snov_dne_list_id'").fetchone()
    if not dne:
        existing = next((b for b in client.blacklists() if b.get("name") == "gtm-tools do-not-email"), None)
        dne_id = existing["id"] if existing else client.create_blacklist("gtm-tools do-not-email")
        led.execute("INSERT INTO settings (key, value) VALUES ('snov_dne_list_id', ?)", (str(dne_id),))
    name = f"{cfg.product.name} - {mailbox} (gtm)"
    lst = next((x for x in client.lists() if x.get("name") == name), None)
    list_id = lst["id"] if lst else client.create_list(name)
    typer.echo(json.dumps({"list_id": list_id, "list_name": name}, indent=2))
    typer.echo(f"""
Now in Snov.io (one time, about 5 minutes):
 1. Settings -> Custom fields: create text fields gtm_subject, gtm_body_1, gtm_body_2, gtm_body_3.
 2. Campaigns -> New email campaign, recipients = list "{name}", sender = the mailbox {mailbox}.
 3. Steps: email 1 subject {{{{gtm_subject}}}}, body {{{{gtm_body_1}}}}; follow-ups after
    {cfg.sending.followup_days[0]} and {cfg.sending.followup_days[1] - cfg.sending.followup_days[0]} days, same thread,
    bodies {{{{gtm_body_2}}}} and {{{{gtm_body_3}}}}. Nothing else in the steps: our footer is inside the fields.
 4. Settings: open tracking OFF, click tracking OFF, "stop for the whole company after a reply" ON,
    do-not-email list "gtm-tools do-not-email", send on business days in the recipient's time zone.
 5. Start the campaign, then run:
    bin/gtm snov link --product {product} --mailbox {mailbox} --list-id {list_id} --campaign-id <id from the URL>
""")


@snov_app.command("link")
def snov_link(product: str = typer.Option(...), mailbox: str = typer.Option(...), list_id: int = typer.Option(...),
              campaign_id: int = typer.Option(...)) -> None:
    """Record the (product, mailbox) -> Snov list + campaign mapping and verify the campaign settings."""
    from gtm.sources import snov
    led = ledger.open_ledger()
    if not led.execute("SELECT 1 FROM mailboxes WHERE id = ?", (mailbox,)).fetchone():
        _fail(f"mailbox {mailbox} not registered: bin/gtm mailbox add (your terminal)")
    ok, why = _verify_campaign(snov.client(), campaign_id)
    led.execute("INSERT INTO snov_campaigns (product, mailbox_id, list_id, campaign_id, settings_ok, created_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(product, mailbox_id) DO UPDATE SET list_id = excluded.list_id, "
                "campaign_id = excluded.campaign_id, settings_ok = excluded.settings_ok",
                (product, mailbox, list_id, campaign_id, int(ok), now_iso()))
    _echo({"linked": True, "settings_verified": ok, "detail": why})
    if not ok:
        raise typer.Exit(1)


def _verify_campaign(client, campaign_id: int) -> tuple[bool, str]:
    try:
        body = client.campaign(campaign_id)
    except Exception as e:
        return False, f"could not read the campaign: {type(e).__name__}"
    data = body.get("data", body) if isinstance(body, dict) else {}
    tracking = data.get("tracking") or {}
    settings = data.get("sending_settings") or {}
    problems = []
    if tracking.get("open") or tracking.get("link_click"):
        problems.append("turn open/click tracking OFF")
    if settings.get("skip_company_domain_who_replied") is not True:
        problems.append("turn ON 'stop for the whole company after a reply'")
    if not tracking and not settings:
        problems.append("the API didn't return the settings; check them in the Snov UI and re-run link")
    return (not problems), ("ok" if not problems else "; ".join(problems))


# ---------------------------------------------------------------- kill switch / auto-send

@sending_app.command("pause")
def sending_pause(reason: str = typer.Option("manual")) -> None:
    """Stop ALL sending now (any product, any mailbox) and pause our Snov campaigns. Anyone may pause."""
    from gtm.handler import pause_campaigns
    led = ledger.open_ledger()
    led.execute("UPDATE sending_state SET paused = 1, reason = ?, at = ?, resume_signature = NULL WHERE id = 1",
                (reason, now_iso()))
    failed: list[str] = []
    if led.execute("SELECT 1 FROM snov_campaigns LIMIT 1").fetchone():
        try:
            from gtm.sources import snov
            failed = pause_campaigns(led, snov.client())
        except Exception as e:
            failed = [f"all ({type(e).__name__})"]
    _echo({"paused": True, "snov_campaigns_to_pause_by_hand": failed})


@sending_app.command("resume")
def sending_resume() -> None:
    """Resume sending. YOUR terminal + PIN. Snov campaigns are resumed in the Snov UI."""
    _human("gtm sending resume")
    key = _unlock()
    who, at = getpass.getuser(), now_iso()
    sig = signing.sign(key, signing.resume_payload(at=at, approved_by=who))
    ledger.open_ledger().execute("UPDATE sending_state SET paused = 0, reason = NULL, at = ?, resume_signature = ? "
                                 "WHERE id = 1", (at, json.dumps({"by": who, "sig": sig})))
    typer.echo("sending resumed. Resume the paused Snov campaigns in the Snov UI.")


@sending_app.command("status")
def sending_status() -> None:
    led = ledger.open_ledger()
    row = led.execute("SELECT * FROM sending_state WHERE id = 1").fetchone()
    _echo({"paused": ledger.sending_paused(led), "reason": row["reason"], "at": row["at"]})


def autosend_eligibility(product: str) -> dict:
    """Code-computed (plan §6.3): >= 14 days since first push, >= 50 reviewed, >= 90% approved unedited,
    bounce < 2%, no complaint."""
    conn = store.open_state(product)
    led = ledger.open_ledger()
    first = conn.execute("SELECT min(pushed_at) FROM sequences WHERE pushed_at IS NOT NULL").fetchone()[0]
    # only approvals whose signature verifies count toward the track record
    signed = [r for r in conn.execute("SELECT * FROM approvals WHERE target = 'sequence' AND via = 'tty_pin'")
              if signing.verify(signing.approval_payload(
                  product=product, target="sequence", target_id=r["target_id"], content_hash=r["content_hash"],
                  approved_by=r["approved_by"], approved_at=r["approved_at"], via="tty_pin",
                  edited=bool(r["edited"])), r["signature"])]
    rejected = conn.execute("SELECT count(*) FROM sequences WHERE status = 'rejected'").fetchone()[0]
    reviewed = len(signed) + rejected
    unedited = sum(1 for r in signed if not r["edited"]) / reviewed if reviewed else 0
    sent = led.execute("SELECT count(*) FROM account_sequences WHERE product = ?", (product,)).fetchone()[0]
    bounced = led.execute("SELECT count(*) FROM account_sequences WHERE product = ? AND status = 'bounced'", (product,)).fetchone()[0]
    complaints = led.execute("SELECT count(*) FROM suppression WHERE reason = 'complaint'").fetchone()[0]
    days = (now() - parse_iso(first)) / timedelta(days=1) if first else 0
    checks = {"days_since_first_push>=14": days >= 14, "reviewed>=50": reviewed >= 50,
              "approved_unedited>=90%": unedited >= 0.9, "bounce<2%": (bounced / sent < 0.02) if sent else False,
              "no_complaints": complaints == 0}
    return {"eligible": all(checks.values()), "checks": checks, "reviewed": reviewed, "days": round(days, 1)}


@autosend_app.command("status")
def autosend_status(product: str = typer.Option(...)) -> None:
    from gtm.outbox import autosend_active
    _echo({"active": autosend_active(ledger.open_ledger(), product), **autosend_eligibility(product)})


@autosend_app.command("enable")
def autosend_enable(product: str = typer.Option(...)) -> None:
    """YOUR terminal + PIN, and only when the track record qualifies."""
    _human("gtm autosend enable")
    elig = autosend_eligibility(product)
    if not elig["eligible"]:
        _echo(elig)
        _fail("not eligible yet")
    key = _unlock()
    who, at = getpass.getuser(), now_iso()
    payload = signing.autosend_payload(product=product, enabled_at=at, approved_by=who)
    blob = json.dumps({"payload": payload, "sig": signing.sign(key, payload)})
    ledger.open_ledger().execute("INSERT INTO autosend (product, enabled, enabled_at, signature) VALUES (?,1,?,?) "
                                 "ON CONFLICT(product) DO UPDATE SET enabled = 1, enabled_at = excluded.enabled_at, "
                                 "signature = excluded.signature", (product, at, blob))
    typer.echo(f"auto-send on for {product}: validated sequences scoring >= auto_send_min_score are approved by policy "
               "(holdout leads never)")


@autosend_app.command("disable")
def autosend_disable(product: str = typer.Option(...)) -> None:
    ledger.open_ledger().execute("UPDATE autosend SET enabled = 0, signature = NULL WHERE product = ?", (product,))
    typer.echo("auto-send off")


# ---------------------------------------------------------------- accounts / clients

@accounts_app.command("clients")
def accounts_clients(product: str = typer.Option(...), estimate: bool = typer.Option(False),
                     confirm: bool = typer.Option(False, help="spend on LinkedIn company data (after --estimate)")) -> None:
    """Import clients.csv + near_miss.csv, profile their sites (free), optionally LinkedIn data (paid)."""
    from gtm import accounts
    from gtm.budget import Budget
    from gtm.run import product_lock
    from gtm.sources import apify
    cfg = config.load(product)
    with product_lock(product):
        conn = store.open_state(product)
        res = accounts.import_clients(conn, product)
        domains = [r[0] for r in conn.execute("SELECT domain FROM accounts WHERE status IN ('client','near_miss')")]
        dream = [r["domain"] for r in accounts.dream_customers(product)]
        domains += [d for d in dream if d not in domains]  # profiled for free like the rest (never paid here)
        if not conn.execute("SELECT 1 FROM clients LIMIT 1").fetchone():
            res["note"] = ("no clients yet: lookalike search uses your dream customers as seeds" if dream else
                           "no clients yet: lookalike search is skipped. Name 5-10 dream customers instead")
        unpaid = [r[0] for r in conn.execute("SELECT domain FROM accounts WHERE status IN ('client','near_miss') "
                                             "AND linkedin_company_checked_at IS NULL")]
        cost = apify.estimate("company", len(unpaid))
        if estimate and not confirm:
            _echo({**res, "accounts": len(domains), "without_linkedin_data": len(unpaid), "linkedin_data_estimate_usd": cost,
                   "next": "re-run with --confirm to buy LinkedIn company data (better lookalikes)"})
            return
        res["profiled"] = accounts.profile_accounts(conn, cfg, domains)
        if confirm:
            res["linkedin"] = (accounts.linkedin_enrich(conn, cfg, unpaid, budget=Budget(max(cost * 1.5, 0.05), 0))
                               if unpaid else {"enriched": 0, "note": "all already have LinkedIn data"})
        res["client_patterns"] = accounts.client_pattern_memories(conn, product)
        res["prescore"] = accounts.rescore_accounts(conn, cfg)
        _echo(res)


@accounts_app.command("build")
def accounts_build(product: str = typer.Option(...), estimate: bool = typer.Option(False),
                   confirm: bool = typer.Option(False, help="spend on LinkedIn company data"),
                   max_new: int = typer.Option(60)) -> None:
    """Build the Target Account List: free wigolo search (ICP + client lookalikes) -> profile -> pre-score.
    --estimate shows the paid part's cost; --confirm buys LinkedIn company data for the new accounts."""
    from gtm import accounts, llm
    from gtm.budget import Budget
    from gtm.run import product_lock
    from gtm.sources import apify
    cfg = config.load(product)
    with product_lock(product):
        conn = store.open_state(product)
        cap = 24 if cfg.usage.profile == "max20x" else 12  # each call classifies one search group or list
        session = llm.Session(conn, run_id=f"tal-{now_iso()[:10]}", cap=cap, model=cfg.usage.model)
        icp_q, look_q = accounts.icp_queries(cfg), accounts.lookalike_queries(conn, product)
        if not icp_q and not look_q:
            _fail("no ICP segments, clients or dream customers yet: run /gtm-icp and /gtm-clients first")
        queries = [("icp", q) for q in icp_q] + [("lookalike", q) for q in look_q]
        found = accounts.candidates_from_search(session, cfg, queries, competitors=cfg.signals.competitors)
        new, hits = [], 0
        for c in found:
            room = (len(new) < max_new
                    and conn.execute("SELECT count(*) FROM accounts").fetchone()[0] < cfg.tal.max_accounts)
            if room and accounts.ensure_account(conn, domain=c["domain"], name=c["name"], source="tal",
                                                evidence=c["evidence"]):
                new.append(c["domain"])
            hits += accounts.record_hits(conn, c)  # known accounts found again still count for the engine
        out = {"queries": {"icp": len(icp_q), "lookalike": len(look_q)}, "candidates": len(found),
               "new_accounts": len(new), "engine_hits": hits, "isolated_calls": session.calls}
        if not look_q:
            out["notes"] = ["lookalike search skipped: no clients yet. Name 5-10 dream customers in /gtm-clients "
                            "to use as lookalike seeds."]
        out["profiled"] = accounts.profile_accounts(conn, cfg, new)
        # the paid part covers every target account still without LinkedIn data, not just this call's new ones
        pending = [r[0] for r in conn.execute(
            "SELECT domain FROM accounts WHERE source = 'tal' AND status IN ('new','prequalified','below_cutoff') "
            "AND linkedin_company_checked_at IS NULL ORDER BY created_at LIMIT ?", (max(max_new, 60),))]
        cost = apify.estimate("company", len(pending))
        out.update(without_linkedin_data=len(pending), linkedin_data_estimate_usd=cost)
        if confirm and pending:
            out["linkedin"] = accounts.linkedin_enrich(conn, cfg, pending,
                                                       budget=Budget(min(cfg.tal.build_budget_usd, cost * 1.5 + 0.05), 0))
        elif pending:
            out["next"] = "re-run with --confirm to buy LinkedIn company data for better pre-scores"
        out["prescore"] = accounts.rescore_accounts(conn, cfg)
        _echo(out)


@accounts_app.command("import")
def accounts_import(product: str = typer.Option(...), file: Path = typer.Option(..., exists=True,
                    help="CSV with company,domain: target accounts you already know")) -> None:
    from gtm import accounts, discovery
    from gtm.util import domain_of
    conn = store.open_state(product)
    n = 0
    for row in accounts._read_csv(file):
        d = domain_of(row.get("domain", ""))
        n += accounts.ensure_account(conn, domain=d, name=row.get("company") or d, source="tal", evidence=f"https://{d}/")
        if d and conn.execute("SELECT 1 FROM accounts WHERE domain = ?", (d,)).fetchone():
            discovery.record(conn, d, "manual", evidence_url=f"https://{d}/", detail=f"imported from {file.name}")
    accounts.profile_accounts(conn, config.load(product), [r[0] for r in conn.execute(
        "SELECT domain FROM accounts WHERE profile_checked_at IS NULL")])
    _echo({"imported": n, "prescore": accounts.rescore_accounts(conn, config.load(product))})


@accounts_app.command("list")
def accounts_list(product: str = typer.Option(...), limit: int = typer.Option(30)) -> None:
    from gtm.util import sanitize_name
    conn = store.open_state(product)
    rows = conn.execute("SELECT domain, name, status, tal_tier, prescore, lookalike_score, source FROM accounts "
                        "ORDER BY prescore DESC NULLS LAST LIMIT ?", (limit,)).fetchall()
    _echo([{**dict(r), "name": sanitize_name(r["name"])} for r in rows])


@accounts_app.command("calibrate")
def accounts_calibrate(product: str = typer.Option(...)) -> None:
    """Leave-one-out check that the pre-score ranks your clients above look-alike strangers."""
    from gtm import accounts
    _echo(accounts.calibrate(store.open_state(product), config.load(product)).__dict__)


# ---------------------------------------------------------------- signal catalog

@signals_app.command("list")
def signals_list(product: str = typer.Option(...)) -> None:
    conn = store.open_state(product)
    catalog.seed(conn)  # codes added after the product was created show up as `proposed`
    _echo([dict(r) for r in conn.execute("SELECT code, status, kind, system, cost_class, cost_note, meaning, "
                                         "freshness_days FROM signal_codes ORDER BY kind, rank")])


def _set_status(product: str, codes: list[str], status: str) -> None:
    conn = store.open_state(product)
    catalog.seed(conn)
    done, refused = [], []
    for c in codes:
        try:
            cur = conn.execute("UPDATE signal_codes SET status = ? WHERE code = ?", (status, c.strip().upper()))
            (done if cur.rowcount else refused).append(c)
        except Exception as e:
            refused.append(f"{c} ({e})")
    _echo({status: done, "refused": refused})


@signals_app.command("confirm")
def signals_confirm(product: str = typer.Option(...), codes: str = typer.Option(..., help="comma-separated")) -> None:
    """Allow these signals to be collected (the founder said yes in /gtm-icp)."""
    _set_status(product, codes.split(","), "confirmed")


@signals_app.command("reject")
def signals_reject(product: str = typer.Option(...), codes: str = typer.Option(...)) -> None:
    _set_status(product, codes.split(","), "rejected")


@signals_app.command("test")
def signals_test(product: str = typer.Option(...),
                 confirm: bool = typer.Option(False, help="also run the paid Apify probes (LinkedIn, X, Instagram)")) -> None:
    """Probe every social source for this product's keywords; recommend on/off per source (plan §15.1 E1).
    Free sources always run; the paid probes print their estimate first and need --confirm."""
    from gtm import social
    from gtm.run import product_lock
    cfg = config.load(product)
    est = social.probe_estimate()
    typer.echo(f"paid probes (Apify, tiny): estimate ${sum(est.values()):.2f} "
               + ("- running" if confirm else "- not run (add --confirm)"))
    with product_lock(product):
        res = social.source_test(product, cfg, store.open_state(product), confirm=confirm)
    typer.echo(f"keywords: {', '.join(res['keywords'])} | isolated calls: {res['isolated_calls']} | "
               f"Apify spent: ${res['apify_usd']:.2f}\n")
    typer.echo(social.format_table(res))
    for n in res["notes"]:
        typer.echo(f"note: {n}")
    typer.echo(f"\nsaved: {res['path']}")


def sheet_cmd(product: str = typer.Option(...), since: int = typer.Option(7, min=1, help="rows found in the last N days"),
              remove: str = typer.Option(None, help="erasure request: drop rows with this post or profile URL for good")) -> None:
    """The intent sheet: anonymous intent posts for YOUR manual follow-up. Nobody on it is emailed."""
    from gtm import social
    from gtm.util import sanitize_name
    cfg = config.load(product)
    if remove:
        from gtm.run import product_lock
        with product_lock(product):
            typer.echo(f"removed {social.remove(product, remove)} row(s); this URL is never added again")
        return
    rows = social.recent_rows(product, since)
    typer.echo(f"intent sheet: {social.sheet_path(product)}")
    typer.echo(f"{len(rows)} row(s) found in the last {since} day(s) (open the CSV in Google Sheets or Excel)")
    for r in rows:
        typer.echo(" | ".join([r["date"], r["source"], r["intent_code"], r["post_url"], r["author_profile_url"] or "-",
                               sanitize_name(r["company_if_named"]) or "-", sanitize_name(r["summary"], 160)]))
    missing = len(social.reddit_rows_missing_author(product))
    ra = cfg.signals.reddit_authors
    if missing or ra.enabled:
        n = min(missing, ra.max_per_run)
        typer.echo(f"Reddit rows without a profile: {missing}. Author lookup (paid, Apify): "
                   f"{'ON' if ra.enabled else 'OFF'}, up to {ra.max_per_run} posts per run, "
                   f"next run about ${social.reddit_lookup_estimate(n):.2f}")


def leads_cmd(product: str = typer.Option(...),
              find_profiles: bool = typer.Option(False, help="free web search for leads with no LinkedIn profile")) -> None:
    """The latest shortlist (companies, shown here) and every lead so far in leads.csv for YOU to open.
    People's names and profiles go only to the file, never to the screen: this is safe in a Claude session."""
    from gtm.report import write_leads_csv
    from gtm.util import sanitize_name
    conn = store.open_state(product)
    run = conn.execute("SELECT run_id FROM shortlist ORDER BY at DESC LIMIT 1").fetchone()
    picked = conn.execute(
        "SELECT s.domain, a.name, s.verdict, s.final, s.reason FROM shortlist s JOIN accounts a ON a.domain = s.domain "
        "WHERE s.run_id = ? AND s.verdict = 'keep' ORDER BY s.final DESC", (run[0],)).fetchall() if run else []
    if find_profiles:
        from gtm import enrich
        from gtm.run import product_lock
        with product_lock(product):
            typer.echo(f"LinkedIn profiles found: {enrich.fill_profiles(conn, limit=50)}")
    path, rows = write_leads_csv(conn, product)
    typer.echo(f"latest shortlist ({run[0] if run else 'none yet'}): {len(picked)} compan{'y' if len(picked) == 1 else 'ies'} kept")
    for p in picked:
        typer.echo(f"  {p['final'] or 0:5.1f}  {sanitize_name(p['name'])} ({p['domain']}): {sanitize_name(p['reason'], 200)}")
    per = {}
    for r in rows:
        per[r["domain"]] = per.get(r["domain"], 0) + 1
    typer.echo(f"\n{len(rows)} lead(s) at {len(per)} compan{'y' if len(per) == 1 else 'ies'}, "
               f"{sum(1 for r in rows if r['linkedin_url'])} with a LinkedIn profile, "
               f"{sum(1 for r in rows if r['email'])} with an email")
    typer.echo(f"leads file: {path} (open it in Google Sheets or Excel; names and profiles are only in the file)")


# ---------------------------------------------------------------- schedule / manual / forget

@schedule_app.command("install")
def schedule_install(product: str = typer.Option(...), feedback: bool = typer.Option(True)) -> None:
    from gtm import schedule
    _echo({"installed": schedule.install(config.load(product), feedback=feedback)})


@schedule_app.command("remove")
def schedule_remove(product: str = typer.Option(...)) -> None:
    from gtm import schedule
    _echo({"removed_lines": schedule.remove(product)})


@schedule_app.command("show")
def schedule_show(product: str = typer.Option(...)) -> None:
    from gtm import schedule
    _echo(schedule.show(product))


@manual_app.command("list")
def manual_list(product: str = typer.Option(None)) -> None:
    from gtm.report import manual_actions
    _echo(manual_actions(ledger.open_ledger(), product))


@manual_app.command("done")
def manual_done(id: int = typer.Argument(...)) -> None:
    ledger.open_ledger().execute("UPDATE manual_actions SET done_at = ? WHERE id = ?", (now_iso(), id))
    typer.echo("marked done")


def forget_cmd(email: str = typer.Argument(...)) -> None:
    """Erase one person everywhere (all products) and suppress them forever. YOUR terminal."""
    _human("gtm forget")
    from gtm import forget
    if input(f"Erase {email} from every product and suppress it forever? Type ERASE: ").strip() != "ERASE":
        _fail("cancelled")
    _echo(forget.forget(email))
