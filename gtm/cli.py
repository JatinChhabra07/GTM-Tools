"""gtm command line. Run via bin/gtm. See plan.md §5 for the phase each command belongs to."""

from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer
import yaml

from gtm import catalog, config, ledger, llm, secrets, signing, store
from gtm.paths import (BadSlug, check_slug, config_dir, data_dir, ledger_db, product_config_dir,
                       products_dir, state_db)

app = typer.Typer(no_args_is_help=True, add_completion=False, help="GTM-Tools outreach automation")
product_app = typer.Typer(no_args_is_help=True, help="Create and inspect products")
secrets_app = typer.Typer(no_args_is_help=True, help="API keys (type them in YOUR terminal)")
keys_app = typer.Typer(no_args_is_help=True, help="Approval signing keys (PIN)")
setup_app = typer.Typer(no_args_is_help=True, help="What the setup wizard still needs")
config_app = typer.Typer(no_args_is_help=True, help="Read/merge product.yaml")
llm_app = typer.Typer(no_args_is_help=True, help="Isolated Claude calls")
tools_app = typer.Typer(no_args_is_help=True, help="Install/verify external tools (last30days, Qdrant, wigolo)")
kb_app = typer.Typer(no_args_is_help=True, help="Per-product knowledge base (mem0)")
app.add_typer(product_app, name="product")
app.add_typer(secrets_app, name="secrets")
app.add_typer(keys_app, name="keys")
app.add_typer(setup_app, name="setup")
app.add_typer(config_app, name="config")
app.add_typer(llm_app, name="llm")
app.add_typer(kb_app, name="kb")
app.add_typer(tools_app, name="tools")


def _fail(msg: str, code: int = 1) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


def _slug(slug: str) -> str:
    try:
        return check_slug(slug)
    except BadSlug as e:
        _fail(str(e))
        raise  # unreachable


def require_human_terminal(action: str) -> None:
    """Human-only actions: a real TTY, and not from inside a Claude Code session.
    The PIN (for signing) is the real guard; this stops accidental use."""
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        _fail(f"`{action}` must be run by you in your own terminal, not inside Claude Code.")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        _fail(f"`{action}` needs an interactive terminal.")


# ---------------------------------------------------------------- product

@product_app.command("create")
def product_create(slug: str = typer.Option(..., help="short id, e.g. acme"),
                   name: str = typer.Option(...), url: str = typer.Option(...)) -> None:
    """Create product.yaml (safe defaults), state.db, and seed the signal catalog."""
    slug = _slug(slug)
    if config.config_path(slug).exists():
        _fail(f"product {slug!r} already exists")
    try:
        cfg = config.ProductConfig(product=config.ProductInfo(slug=slug, name=name, url=url))
    except Exception as e:  # pydantic error text is readable
        _fail(f"invalid product: {e}")
    path = config.save(cfg)
    conn = store.open_state(slug)
    added = catalog.seed(conn)
    ledger.open_ledger().close()
    typer.echo(json.dumps({"created": slug, "config": str(path), "state_db": str(state_db(slug)),
                           "signal_codes_seeded": added}, indent=2))


@product_app.command("list")
def product_list() -> None:
    root = products_dir()
    slugs = sorted(p.name for p in root.iterdir() if (p / "product.yaml").exists()) if root.exists() else []
    typer.echo(json.dumps(slugs))


@product_app.command("show")
def product_show(product: str = typer.Option(...)) -> None:
    """The first thing every skill runs: proves which product the session is on."""
    cfg = config.load(_slug(product))
    typer.echo(f"PRODUCT: {cfg.product.slug} | {cfg.product.name} | {cfg.product.url}")


# ---------------------------------------------------------------- config

@config_app.command("get")
def config_get(product: str = typer.Option(...)) -> None:
    cfg = config.load(_slug(product))
    typer.echo(yaml.safe_dump(cfg.model_dump(mode="json", exclude_none=True), sort_keys=False))


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@config_app.command("merge")
def config_merge(product: str = typer.Option(...),
                 file: Path = typer.Option(..., exists=True, help="YAML with the keys to change")) -> None:
    """Merge answers from the setup wizard into product.yaml. Validated; refuses unsafe changes."""
    slug = _slug(product)
    current = config.load(slug).model_dump(mode="json", exclude_none=True)
    patch = yaml.safe_load(file.read_text()) or {}
    if not isinstance(patch, dict):
        _fail("patch must be a YAML mapping")
    if "product" in patch and patch["product"].get("slug", slug) != slug:
        _fail("the product slug can't be changed")
    try:
        cfg = config.ProductConfig.model_validate(_deep_merge(current, patch))
    except Exception as e:
        _fail(f"rejected: {e}")
    config.save(cfg)
    typer.echo(f"updated {config.config_path(slug)}")


# ---------------------------------------------------------------- secrets

@secrets_app.command("set")
def secrets_set(name: str = typer.Argument(None, help="one key, or omit to be asked for each")) -> None:
    """Type API keys here, in YOUR terminal. Input is hidden and never shown again."""
    require_human_terminal("gtm secrets set")
    names = [name] if name else list(secrets.KNOWN)
    for key in names:
        if key not in secrets.KNOWN:
            _fail(f"unknown key {key}")
        need, help_text = secrets.KNOWN[key]
        typer.echo(f"\n{key} ({need}): {help_text}")
        value = getpass.getpass("  paste value (Enter to skip): ")
        if value.strip():
            secrets.set_value(key, value)
            typer.echo("  saved")


@secrets_app.command("status")
def secrets_status() -> None:
    """Which keys are set. Names only, never values."""
    typer.echo(json.dumps({"file_mode_ok": secrets.file_mode_ok(), "keys": secrets.status()}, indent=2))


# ---------------------------------------------------------------- signing keys

@keys_app.command("init")
def keys_init() -> None:
    """Create your approval PIN + signing key. Only you should know the PIN."""
    require_human_terminal("gtm keys init")
    if signing.keys_exist():
        _fail("keys already exist; use `bin/gtm keys change-pin`")
    typer.echo(f"Choose an approval PIN (at least {signing.MIN_PIN_LEN} characters). "
               "You'll type it when you review emails. Never share it or type it into chat.")
    pin = getpass.getpass("  PIN: ")
    if pin != getpass.getpass("  repeat PIN: "):
        _fail("PINs don't match")
    try:
        signing.init_keys(pin)
    except signing.SigningError as e:
        _fail(str(e))
    typer.echo("signing key created")


@keys_app.command("change-pin")
def keys_change_pin() -> None:
    require_human_terminal("gtm keys change-pin")
    old = getpass.getpass("  current PIN: ")
    new = getpass.getpass("  new PIN: ")
    if new != getpass.getpass("  repeat new PIN: "):
        _fail("PINs don't match")
    try:
        signing.change_pin(old, new)
    except signing.SigningError as e:
        _fail(str(e))
    typer.echo("PIN changed")


# ---------------------------------------------------------------- isolated LLM

@llm_app.command("ping")
def llm_ping() -> None:
    """One isolated, tool-less call, to prove the subscription + isolation work."""
    try:
        res = llm.call("Reply with exactly: OK")
    except llm.LLMError as e:
        _fail(f"{type(e).__name__}: {e}")
    typer.echo(json.dumps({"result": res.text, "model": res.model, "duration_ms": res.duration_ms}))


# ---------------------------------------------------------------- tools

@tools_app.command("install")
def tools_install() -> None:
    """Install everything the automation runs on: last30days (pinned), Qdrant, the embedder, wigolo."""
    from gtm import kb
    from gtm.paths import REPO_ROOT
    from gtm.sources import last30days, wigolo
    out: dict = {}
    try:
        out["wigolo"] = {**wigolo.warm(),
                         "claude_code": wigolo.register_project(REPO_ROOT / ".mcp.json",
                                                                already_listed=mcp_listed("wigolo"))}
    except Exception as e:  # npx missing or network: report, don't crash the installer
        out["wigolo"] = {"error": str(e)[:300]}
    try:
        out["last30days"] = last30days.install()
    except last30days.Last30Error as e:
        out["last30days"] = {"error": str(e)}
    try:
        out["qdrant"] = kb.ensure_qdrant()
    except kb.KBError as e:
        out["qdrant"] = {"error": str(e)}
    try:
        from sentence_transformers import SentenceTransformer
        SentenceTransformer(kb.EMBED_MODEL)  # downloads once into the HF cache
        out["embedder"] = kb.EMBED_MODEL
    except Exception as e:  # network or disk: report, don't crash the installer
        out["embedder"] = {"error": str(e)[:300]}
    typer.echo(json.dumps(out, indent=2))
    if any(isinstance(v, dict) and "error" in v for v in out.values()):
        raise typer.Exit(1)


@tools_app.command("check")
def tools_check(paid: bool = typer.Option(False, help="also check the Apify and Snov keys (free reads, no credits)")) -> None:
    """Live tool truth: one wigolo search/fetch/diff from Python and one last30days run."""
    from gtm.sources import last30days, wigolo
    out: dict = {}
    if paid:
        from gtm.sources import apify, snov
        try:
            spend = apify.client().month_spend_usd()  # None = the key didn't work
            out["apify"] = {"ok": spend is not None, "month_spend_usd": spend}
        except Exception as e:
            out["apify"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        try:
            out["snov"] = {"ok": True, "balance": snov.client().balance()}
        except Exception as e:
            out["snov"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    try:
        res = wigolo.search(["b2b saas pricing page"], max_results=2)
        out["wigolo_search"] = {"ok": bool(res.get("results")), "results": len(res.get("results", []))}
        page = wigolo.fetch("https://example.com", max_content_chars=300)
        out["wigolo_fetch"] = {"ok": bool(page.get("markdown"))}
        d = wigolo.diff({"markdown": "Plan: $49"}, {"markdown": "Plan: $59"}, output="summary")
        out["wigolo_diff"] = {"ok": d.get("changed") is True}
    except Exception as e:
        out["wigolo"] = {"ok": False, "error": str(e)[:300]}
    try:
        exp = last30days.run("crm for small business", timeout=240)
        out["last30days"] = {"ok": True, "items": len(last30days.items(exp)),
                             "sources": exp.get("source_status", {})}
    except Exception as e:
        out["last30days"] = {"ok": False, "error": str(e)[:300]}
    typer.echo(json.dumps(out, indent=2, default=str))


# ---------------------------------------------------------------- knowledge base

def _kb():
    from gtm import kb
    return kb


@kb_app.command("up")
def kb_up() -> None:
    """Start the local Qdrant server (docker) if it isn't running."""
    k = _kb()
    try:
        typer.echo(json.dumps({"qdrant": k.ensure_qdrant(), "url": k.QDRANT_URL}))
    except k.KBError as e:
        _fail(str(e))


@kb_app.command("add")
def kb_add(product: str = typer.Option(...), category: str = typer.Option(...),
           text: str = typer.Option(...), source_url: str = typer.Option(None)) -> None:
    k = _kb()
    try:
        typer.echo(json.dumps(k.add(_slug(product), category, text, source_url=source_url)))
    except k.KBError as e:
        _fail(str(e))


@kb_app.command("ingest")
def kb_ingest(product: str = typer.Option(...),
              file: Path = typer.Option(..., exists=True, help="JSON list or JSONL of {category,text,source_url}"),
              keep: bool = typer.Option(True, help="also append to knowledge/extracted.jsonl for rebuilds"),
              replace: bool = typer.Option(False, help="drop every earlier site fact first (re-onboarding)")) -> None:
    """Add facts extracted from the product site (authority=site). Human facts are never touched."""
    k = _kb()
    slug = _slug(product)
    raw = file.read_text().strip()
    items = json.loads(raw) if raw.startswith("[") else [json.loads(x) for x in raw.splitlines() if x.strip()]
    removed = k.drop_site_facts(slug) if replace else 0
    res = k.ingest(slug, items)
    res["replaced"] = removed
    if keep:
        ex = k.extracted_path(slug)
        ex.parent.mkdir(parents=True, exist_ok=True)
        with ex.open("w" if replace else "a") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
    typer.echo(json.dumps(res, indent=2))


@kb_app.command("search")
def kb_search(product: str = typer.Option(...), query: str = typer.Option(...),
              category: str = typer.Option(None), k: int = typer.Option(5)) -> None:
    """Search one product's KB. pain_language text is redacted: it is scraped, untrusted text."""
    hits = _kb().search(_slug(product), query, category=category, k=k)
    for h in hits:
        if h.get("category") == "pain_language":
            h["text"] = "[redacted: scraped text, used only inside isolated calls]"
        h.pop("dedupe", None)
    typer.echo(json.dumps(hits, indent=2, ensure_ascii=False))


@kb_app.command("reload-facts")
def kb_reload_facts(product: str = typer.Option(...)) -> None:
    """Re-load products/<slug>/facts.yaml: the only way human facts change."""
    typer.echo(json.dumps(_kb().reload_facts(_slug(product))))


@kb_app.command("rebuild")
def kb_rebuild(product: str = typer.Option(...)) -> None:
    """Wipe and rebuild this product's KB from knowledge/extracted.jsonl + facts.yaml + learnings.jsonl."""
    typer.echo(json.dumps(_kb().rebuild(_slug(product)), indent=2))


@kb_app.command("stats")
def kb_stats(product: str = typer.Option(...)) -> None:
    typer.echo(json.dumps(_kb().stats(_slug(product)), indent=2))


# ---------------------------------------------------------------- setup + doctor

def _qdrant_ready() -> bool:
    from gtm import kb
    return kb.qdrant_ready()


def mcp_listed(name: str) -> bool | None:
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "mcp", "list"], capture_output=True, text=True, timeout=90).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return any(line.split(":")[0].strip().endswith(name) for line in out.splitlines())


def _tool_checks() -> dict[str, dict]:
    """Every tool/skill the plan relies on (CLAUDE.md 'Tool & skill map')."""
    home = Path.home()
    skills = home / ".claude" / "skills"

    plugins = home / ".claude" / "plugins"

    def skill(name: str) -> bool:
        if (skills / name / "SKILL.md").exists():
            return True
        return plugins.exists() and any(plugins.glob(f"**/skills/{name}/SKILL.md"))

    from gtm.sources import last30days
    try:
        llm.probe_isolation()
        isolation = True
    except llm.IsolationUnavailable:
        isolation = False
    return {
        "claude_isolation": {"ok": isolation, "used_for": "all judgement on untrusted text"},
        "wigolo_mcp": {"ok": mcp_listed("wigolo"), "used_for": "crawl product site, web/news search, page diffs"},
        "graph_engineering_skill": {"ok": skill("graph-engineering"), "used_for": "qualification graph pattern"},
        "human_voice_skill": {"ok": skill("human-voice"), "used_for": "anti-AI-tell rules for the humanize pass"},
        "last30days_engine": {"ok": last30days.installed(),
                              "used_for": "Reddit/HN pain language (bin/gtm tools install)"},
        "docker": {"ok": shutil.which("docker") is not None, "used_for": "runs the Qdrant server"},
        "qdrant": {"ok": _qdrant_ready(), "used_for": "mem0 knowledge base (bin/gtm kb up)"},
        "apify_key": {"ok": secrets.status()["APIFY_API_TOKEN"] == "set", "used_for": "LinkedIn/X, people search"},
        "snov_keys": {"ok": secrets.status()["SNOV_CLIENT_ID"] == "set"
                      and secrets.status()["SNOV_CLIENT_SECRET"] == "set",
                      "used_for": "email find/verify, sending"},
        "firecrawl_key": {"ok": secrets.status()["FIRECRAWL_API_KEY"] == "set",
                          "used_for": "optional fallback crawler"},
    }


def _progress(slug: str, cfg) -> dict:
    """Where this product is in the setup journey. Each probe is independent: one failure doesn't hide the rest."""
    from gtm import ledger, store
    out: dict = {}
    conn = store.open_state(slug)
    out["signals_confirmed"] = [r[0] for r in conn.execute(
        "SELECT code FROM signal_codes WHERE status = 'confirmed' AND kind IN ('intent','trigger')")]
    out["clients"] = conn.execute("SELECT count(*) FROM clients").fetchone()[0]
    out["accounts"] = conn.execute("SELECT count(*) FROM accounts WHERE status NOT IN ('client','near_miss')").fetchone()[0]
    from gtm import kb
    out["kb_facts"] = kb.count_fast(slug)
    if out["kb_facts"] is None:
        out["kb_error"] = "Qdrant isn't answering: bin/gtm kb up"
    led = ledger.open_ledger()
    out["mailboxes"] = [{"id": r["id"], "ready": r["ready_state"], "paused": bool(r["paused"])}
                        for r in led.execute("SELECT id, ready_state, paused FROM mailboxes")]
    out["snov_linked"] = bool(led.execute("SELECT 1 FROM snov_campaigns WHERE product = ? AND settings_ok = 1",
                                          (slug,)).fetchone())
    try:
        from gtm import schedule
        out["schedule"] = len(schedule.show(slug))
    except Exception:
        out["schedule"] = None
    out["tools"] = {"last30days": __import__("gtm.sources.last30days", fromlist=["x"]).installed(),
                    "qdrant": _qdrant_ready()}
    return out


@setup_app.command("status")
def setup_status(product: str = typer.Option(None)) -> None:
    """JSON checklist the /gtm-setup wizard reads to decide what to ask next. No secret values."""
    out: dict = {"signing_keys": signing.keys_exist(), "secrets": secrets.status(),
                 "secrets_file_mode_ok": secrets.file_mode_ok(), "products": []}
    root = products_dir()
    if root.exists():
        out["products"] = sorted(p.name for p in root.iterdir() if (p / "product.yaml").exists())
    if product:
        slug = _slug(product)
        missing = []
        try:
            cfg = config.load(slug)
        except FileNotFoundError:
            missing.append("product")
            cfg = None
        if cfg:
            if not cfg.personas:
                missing.append("personas (run /gtm-icp)")
            if not cfg.icp.segments:
                missing.append("icp segments (run /gtm-icp)")
            if not cfg.voice.sender_name:
                missing.append("voice.sender_name")
            if not cfg.voice.postal_address:
                missing.append("voice.postal_address (US law needs it before sending)")
            clients_csv = product_config_dir(slug) / "clients.csv"
            if not clients_csv.exists():
                missing.append(f"clients.csv at {clients_csv} (run /gtm-clients)")
        progress = _progress(slug, cfg) if cfg else {}
        if cfg:
            if not progress.get("signals_confirmed"):
                missing.append("confirmed intent/trigger signals (run /gtm-icp)")
            if not progress.get("kb_facts"):
                missing.append("product knowledge (run /gtm-onboard)")
            if not progress.get("accounts"):
                missing.append("target accounts (run /gtm-accounts)")
            if not progress.get("mailboxes"):
                missing.append("a sending mailbox (founder runs `bin/gtm mailbox add` in their own terminal)")
            elif not cfg.sending.mailboxes:
                missing.append("sending.mailboxes in product config (which registered mailbox this product uses)")
            if cfg.sending.mailboxes and not progress.get("snov_linked"):
                missing.append("Snov campaign link (bin/gtm snov setup, then snov link)")
            if not progress.get("schedule"):
                missing.append("schedule (bin/gtm schedule install)")
        out["product"] = {"slug": slug, "missing": missing, "progress": progress,
                          "usage_profile": cfg.usage.profile if cfg else None,
                          "geo": {k: v.status for k, v in cfg.geo_policy.items()} if cfg else None}
    if not out["signing_keys"]:
        out.setdefault("human_actions", []).append("run `bin/gtm keys init` in your own terminal")
    if any(v == "missing" and secrets.KNOWN[k][0] == "required" for k, v in out["secrets"].items()):
        out.setdefault("human_actions", []).append("run `bin/gtm secrets set` in your own terminal")
    typer.echo(json.dumps(out, indent=2))


@app.command()
def doctor(product: str = typer.Option(None), tools: bool = typer.Option(True)) -> None:
    """Health check. Global by default; --product adds per-product checks."""
    checks: dict[str, dict] = {}
    for d, label in ((data_dir(), "data_dir"), (config_dir(), "config_dir")):
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write-test"
            probe.write_text("x")
            probe.unlink()
            ok = not str(d.resolve()).startswith("/mnt/windows")
            checks[label] = {"ok": ok, "path": str(d), "note": "" if ok else "must not be on the NTFS mount"}
        except OSError as e:
            checks[label] = {"ok": False, "path": str(d), "note": str(e)}
    try:
        ledger.open_ledger().close()
        checks["ledger_db"] = {"ok": True, "path": str(ledger_db())}
    except Exception as e:
        checks["ledger_db"] = {"ok": False, "note": str(e)}
    checks["signing_keys"] = {"ok": signing.keys_exist(), "note": "bin/gtm keys init (your terminal)"}
    checks["secrets_file_mode"] = {"ok": secrets.file_mode_ok()}
    if tools:
        checks.update(_tool_checks())
    if product:
        slug = _slug(product)
        try:
            cfg = config.load(slug)
            conn = store.open_state(slug)
            catalog.seed(conn)
            n = conn.execute("SELECT count(*) FROM signal_codes").fetchone()[0]
            checks["product"] = {"ok": True, "slug": cfg.product.slug, "signal_codes": n}
        except Exception as e:
            checks["product"] = {"ok": False, "note": str(e)}
    typer.echo(json.dumps(checks, indent=2))
    hard = ("data_dir", "config_dir", "ledger_db", "secrets_file_mode", "claude_isolation", "product")
    if any(checks[k]["ok"] is False for k in hard if k in checks):
        raise typer.Exit(1)


from gtm import cli_ops  # noqa: E402  (operational commands; registered last to keep this file readable)

cli_ops.register(app)

if __name__ == "__main__":
    app()
