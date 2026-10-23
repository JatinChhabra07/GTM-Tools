"""`gtm competitors ...` (plan §15.1 E2): analyse competitors, then turn their customers into accounts.

Free first, paid only after the founder said yes: `clients` always runs the free part (their
customer pages) and prices the paid part; `--confirm` buys it (review sites, LinkedIn/X posts),
capped by `tal.build_budget_usd`. Registered on the main app in gtm/cli.py.
"""

from __future__ import annotations

import json

import typer

from gtm import catalog, config, store
from gtm.util import days_ago, now_iso

competitors_app = typer.Typer(no_args_is_help=True, help="Competitor briefs and their customers (accounts)")
FREE_WATERMARK = "competitors_cli_free"
CODES = ("COMPETITOR_CUSTOMER", "COMPETITOR_REVIEW_NEGATIVE")


def _echo(obj) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


def _fail(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _open(product: str):
    conn = store.open_state(product)
    catalog.seed(conn)  # products created before these codes existed get them as `proposed`
    return conn


def _codes(conn) -> dict:
    return {r["code"]: r["status"] for r in conn.execute(
        f"SELECT code, status FROM signal_codes WHERE code IN ({','.join('?' * len(CODES))})", CODES)}


def _session(conn, cfg, calls: int):
    from gtm import llm
    return llm.Session(conn, run_id=f"competitors-{now_iso()[:10]}", cap=calls, model=cfg.usage.model,
                       pause_minutes=cfg.usage.usage_limit_pause_minutes)


@competitors_app.command("list")
def competitors_list(product: str = typer.Option(...)) -> None:
    """The competitors in the config, what was analysed, and the accounts found through each."""
    from gtm import competitors
    cfg = config.load(product)
    conn = _open(product)
    briefs = {(b.get("entry") or "").lower(): b for b in competitors.load_all(product)}
    rows = []
    for entry in cfg.signals.competitors:
        b = briefs.get(entry.strip().lower(), {})
        name = b.get("name") or entry
        rows.append({"entry": entry, "name": name, "domain": b.get("domain"), "analyzed_at": b.get("analyzed_at"),
                     "facts": len(b.get("facts", [])) + len(b.get("review_facts", [])),
                     "brief": str(competitors.brief_dir(product) / f"{competitors.name_slug(name)}.md") if b else None,
                     "review_pages": b.get("review_pages", {}), "script_hosts": [h["host"] for h in b.get("script_hosts", [])],
                     "accounts_using_it": conn.execute(
                         "SELECT count(DISTINCT account_domain) FROM observations WHERE signal_code = "
                         "'COMPETITOR_CUSTOMER' AND lower(value_text) = lower(?)", (name,)).fetchone()[0]})
    _echo({"competitors": rows, "codes": _codes(conn)})


@competitors_app.command("analyze")
def competitors_analyze(product: str = typer.Option(...),
                        name: str = typer.Option(None, help="only this competitor (as listed in the config)")) -> None:
    """Free: crawl each competitor's site and review pages -> a sourced brief + `competitor` KB facts.
    About 2 isolated calls per competitor (1 when its domain is already known)."""
    from gtm import competitors, llm
    from gtm.run import product_lock
    cfg = config.load(product)
    entries = [e for e in cfg.signals.competitors if not name or e.strip().lower() == name.strip().lower()]
    if not entries:
        _fail("no competitor to analyse: list them first (signals.competitors, via /gtm-competitors)")
    entries = entries[:competitors.MAX_COMPETITORS]
    with product_lock(product):
        conn = _open(product)
        session = _session(conn, cfg, 6 * len(entries))  # 2 calls each, room for retries
        notes = competitors.Notes()
        out = []
        for e in entries:
            try:
                out.append(competitors.analyze(conn, session, cfg, e, notes))
            except (llm.CallCapReached, llm.UsageLimit) as err:
                notes.note(f"stopped before {e}: {err}")
                break
        _echo({"competitors": out, "isolated_calls": session.calls, "notes": notes.notes})


@competitors_app.command("clients")
def competitors_clients(product: str = typer.Option(...),
                        estimate: bool = typer.Option(False, help="price the paid part, buy nothing"),
                        confirm: bool = typer.Option(False, help="buy the paid part (review sites, LinkedIn/X posts)"),
                        name: str = typer.Option(None, help="only this competitor")) -> None:
    """Companies that use the competitors -> target accounts (source 'competitor', evidence URL each).
    Free: their customer / case-study / testimonial pages. Paid (--confirm): G2/Capterra/Trustpilot
    reviews and LinkedIn/X posts, priced first (--estimate). Only confirmed signal codes are recorded."""
    from gtm import accounts, competitors, llm
    from gtm.budget import Budget, BudgetExceeded
    from gtm.run import product_lock
    from gtm.sources import apify, optional
    if estimate and confirm:
        _fail("use --estimate OR --confirm")
    cfg = config.load(product)
    with product_lock(product):
        conn = _open(product)
        briefs = [b for b in competitors.current(product, cfg)
                  if not name or name.strip().lower() in ((b.get("entry") or "").lower(), b["name"].lower())]
        if not briefs:
            _fail("no analysed competitor yet: run `bin/gtm competitors analyze --product <slug>` first")
        notes = competitors.Notes()
        session = _session(conn, cfg, 6 * len(briefs) + 10)
        out: dict = {"codes": _codes(conn), "free": [], "notes": notes.notes}
        row = conn.execute("SELECT last_collected_at FROM watermarks WHERE source = ?", (FREE_WATERMARK,)).fetchone()
        try:
            if not (confirm and row and days_ago(row[0]) < 1):  # --confirm right after --estimate: don't repeat it
                for b in briefs:
                    out["free"].append(competitors.customers_free(conn, session, cfg, b, notes))
                conn.execute("INSERT INTO watermarks (source, last_collected_at) VALUES (?, ?) ON CONFLICT(source) "
                             "DO UPDATE SET last_collected_at = excluded.last_collected_at", (FREE_WATERMARK, now_iso()))
            plan = competitors.paid_plan(conn, cfg, briefs)
            total = round(sum(p["usd"] for p in plan), 2)
            out.update(paid_plan=[{k: p[k] for k in ("what", "items", "usd")} for p in plan], paid_estimate_usd=total)
            if not plan:
                out["paid"] = ("nothing to buy: confirm COMPETITOR_CUSTOMER and/or COMPETITOR_REVIEW_NEGATIVE first "
                               "(and analyse the competitors so their review pages are known)")
            elif confirm:
                budget = Budget(min(cfg.tal.build_budget_usd, round(total * 1.5 + 0.05, 2)), 0)
                client = optional(notes, "Apify (review sites, posts)", apify.client)
                if client is not None:
                    try:
                        out["reviews"] = [optional(notes, f"reviews of {b['name']}", competitors.reviews, conn, session,
                                                   cfg, b, plan, client, budget, notes) for b in briefs]
                        out["posts"] = optional(notes, "competitor posts", competitors.posts, conn, session, cfg, briefs,
                                                plan, client, budget, notes, f"competitors-{now_iso()[:10]}")
                    except BudgetExceeded as e:
                        notes.note(str(e))
                out["spent"] = budget.summary()
            else:
                out["next"] = f"re-run with --confirm to buy it (about ${total:.2f}, capped at ${cfg.tal.build_budget_usd:.2f})"
        except (llm.CallCapReached, llm.UsageLimit) as e:
            notes.note(f"stopped early: {e}")
        fresh = [r[0] for r in conn.execute("SELECT domain FROM accounts WHERE source = 'competitor' AND "
                                            "profile_checked_at IS NULL LIMIT 40")]
        out["profiled"] = accounts.profile_accounts(conn, cfg, fresh) if fresh else 0
        out["prescore"] = accounts.rescore_accounts(conn, cfg)
        out["accounts_from_competitors"] = conn.execute(
            "SELECT count(*) FROM accounts WHERE source = 'competitor'").fetchone()[0]
        out["isolated_calls"] = session.calls
        _echo(out)
