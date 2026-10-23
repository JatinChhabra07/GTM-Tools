"""`gtm run`: one process per product run, fixed step order (plan §1.1, §5 Phase 8).

 1 sync      replies, bounces, unsubscribes (if this fails: NO pushes this run)
 2 drain     approved + signature-valid + hash-matching + re-gated sequences -> Snov
 3 collect   triggers (TAL accounts) + signals (social)
 4 resolve   explicit evidence only -> accounts; pre-score
 5 enrich    personas -> email pattern-first -> verify (within the daily cap)
 6 qualify   graph: worker -> lens reviewers -> synth -> gate
 7 write     FACTS-only writer -> humanize -> validator -> pending_review
 8 report

Resume (full/refresh only): the latest run of the same mode that is `running` (its process died:
we hold the lock) or `failed` with attempt < 2, started in the last 24 h, is resumed. Steps 3-7 that
finished are skipped; steps 1-2 always re-run so a fresh reply sync precedes any push.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import traceback
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from gtm import config, ledger, llm, store
from gtm.budget import Budget, BudgetExceeded
from gtm.paths import ensure_private_dir, product_data_dir, runs_dir
from gtm.util import new_run_id, now, now_iso, parse_iso

ALWAYS = ("sync", "drain")
PIPELINE = ("collect", "resolve", "enrich", "qualify", "write")
MODES = ("full", "refresh", "sync", "noop")


class AlreadyRunning(Exception):
    pass


class StepFailed(Exception):
    pass


@contextlib.contextmanager
def product_lock(slug: str) -> Iterator[None]:
    """Held for the WHOLE run; every mutating command takes the same lock (plan §2.1)."""
    path = ensure_private_dir(product_data_dir(slug)) / "lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning(f"{slug}: another gtm run/command holds the product lock") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@dataclass
class Ctx:
    slug: str
    cfg: config.ProductConfig
    conn: sqlite3.Connection
    ledger: sqlite3.Connection
    run_id: str
    mode: str
    run_dir: Path
    llm: llm.Session
    budget: Budget
    limit: int | None = None
    counts: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    resumed: bool = False
    sync_ok: bool = False
    partial_reason: str | None = None

    def count(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    # external refs are saved BEFORE waiting on a paid async job (Apify run id, Snov task hash)
    def get_ref(self, step: str) -> dict:
        row = self.conn.execute("SELECT external_ref FROM steps WHERE run_id = ? AND step = ?",
                                (self.run_id, step)).fetchone()
        return json.loads(row[0]) if row and row[0] else {}

    def set_ref(self, step: str, key: str, value) -> None:
        refs = self.get_ref(step)
        refs[key] = value
        self.conn.execute(
            "INSERT INTO steps (run_id, step, status, external_ref, started_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(run_id, step) DO UPDATE SET external_ref = excluded.external_ref",
            (self.run_id, step, "running", json.dumps(refs), now_iso()))

    def step_done(self, step: str) -> bool:
        row = self.conn.execute("SELECT status FROM steps WHERE run_id = ? AND step = ?",
                                (self.run_id, step)).fetchone()
        return bool(row and row[0] == "done")

    def mark(self, step: str, status: str) -> None:
        self.conn.execute(
            "INSERT INTO steps (run_id, step, status, started_at) VALUES (?,?,?,?) "
            "ON CONFLICT(run_id, step) DO UPDATE SET status = excluded.status, "
            "finished_at = CASE WHEN excluded.status IN ('done','failed') THEN ? ELSE finished_at END",
            (self.run_id, step, status, now_iso(), now_iso()))


def daily_cap(ctx: Ctx) -> int:
    """Leads/day = Σ(mailbox effective cap × product share) / touches per lead (plan §4.2).
    Before any mailbox is READY, --limit (default 10) is the cap."""
    from gtm import warmup
    total = 0.0
    for share in ctx.cfg.sending.mailboxes:
        mb = ctx.ledger.execute("SELECT * FROM mailboxes WHERE id = ?", (share.id,)).fetchone()
        if not mb:
            continue
        total += warmup.effective_daily_cap(
            warmup_start=mb["warmup_start"], warmup_min_days=mb["warmup_min_days"],
            start_sends=mb["start_sends"], daily_increase=mb["daily_increase"], daily_cap=mb["daily_cap"],
            ready_state=mb["ready_state"], paused=bool(mb["paused"]), on_date=now().date()) * share.share
    cap = int(total // max(1, ctx.cfg.sending.touches))
    if cap <= 0:
        cap = ctx.limit if ctx.limit is not None else 10
    elif ctx.limit is not None:
        cap = min(cap, ctx.limit)
    return cap


def enrich_quota(ctx: Ctx) -> int:
    """N = cap - enriched today - max(0, backlog - cap); 0 when the review backlog is >= 2x cap."""
    cap = daily_cap(ctx)
    today = now().date().isoformat()
    enriched = ctx.conn.execute("SELECT count(*) FROM leads WHERE enriched_at >= ?", (today,)).fetchone()[0]
    backlog = ctx.conn.execute("SELECT count(*) FROM sequences WHERE status = 'pending_review'").fetchone()[0]
    if backlog >= 2 * cap:
        ctx.note(f"review backlog {backlog} >= 2x daily cap {cap}: prospecting skipped (approve or reject first)")
        return 0
    return max(0, cap - enriched - max(0, backlog - cap))


def _resumable(conn: sqlite3.Connection, mode: str) -> sqlite3.Row | None:
    if mode not in ("full", "refresh"):
        return None
    row = conn.execute("SELECT * FROM runs WHERE mode = ? ORDER BY started_at DESC LIMIT 1", (mode,)).fetchone()
    if not row:
        return None
    recent = now() - parse_iso(row["started_at"]) <= timedelta(hours=24)
    if recent and (row["status"] == "running" or (row["status"] == "failed" and row["attempt"] < 2)):
        return row
    return None


def _stages():
    from gtm import enrich, handler, outbox, qualify, resolve, signals, triggers, writer
    return {
        "sync": handler.sync,
        "drain": outbox.drain,
        "collect": lambda ctx: (triggers.collect(ctx), signals.collect(ctx)),
        "resolve": resolve.run,
        "enrich": enrich.run,
        "qualify": qualify.run,
        "write": writer.run,
    }


def _noop(ctx: Ctx) -> None:
    """Phase 0 exit check: 1 isolated call + 1 wigolo call + 1 SQLite write, no prompts."""
    from pydantic import BaseModel

    from gtm.sources import wigolo

    class Pong(BaseModel):
        ok: bool

    res = ctx.llm.json("noop", {"say": "ok"}, Pong)
    ctx.counts["llm_ok"] = bool(res and res.ok)
    stats = wigolo.call("cache", {"stats": True})
    ctx.counts["wigolo_ok"] = isinstance(stats, dict)
    ctx.mark("noop", "done")


def execute(slug: str, mode: str, *, limit: int | None = None) -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    cfg = config.load(slug)
    with product_lock(slug):
        conn = store.open_state(slug)
        led = ledger.open_ledger()
        prev = _resumable(conn, mode)
        if prev:
            run_id, attempt = prev["run_id"], prev["attempt"] + 1
            conn.execute("UPDATE runs SET status = 'running', attempt = ?, failure_reason = NULL "
                         "WHERE run_id = ?", (attempt, run_id))
        else:
            run_id, attempt = new_run_id(), 1
            conn.execute("INSERT INTO runs (run_id, mode, started_at, status, attempt) VALUES (?,?,?,?,1)",
                         (run_id, mode, now_iso(), "running"))
        run_dir = ensure_private_dir(runs_dir(slug) / run_id)
        limits = cfg.usage.limits()
        ctx = Ctx(slug=slug, cfg=cfg, conn=conn, ledger=led, run_id=run_id, mode=mode, run_dir=run_dir,
                  llm=llm.Session(conn, run_id=run_id, cap=limits["llm_calls_per_run"],
                                  model=cfg.usage.model, pause_minutes=cfg.usage.usage_limit_pause_minutes),
                  budget=Budget(cfg.budgets.apify_usd_per_run, cfg.budgets.snov_credits_per_run),
                  limit=limit, resumed=bool(prev))
        status, reason = "done", None
        try:
            if mode == "noop":
                _noop(ctx)
            else:
                stages = _stages()
                for step in ALWAYS:
                    if step == "drain" and not ctx.sync_ok:
                        ctx.note("reply sync failed or skipped: nothing pushed this run")
                        continue
                    _run_step(ctx, step, stages[step])
                if mode in ("full", "refresh"):
                    for step in PIPELINE:
                        if ctx.step_done(step):
                            ctx.note(f"resume: {step} already done")
                            continue
                        _run_step(ctx, step, stages[step])
        except (BudgetExceeded, llm.CallCapReached, llm.UsageLimit) as e:
            status, reason = "partial", f"{type(e).__name__}: {e}"
        except Exception as e:  # recorded, reported, exit non-zero
            status, reason = "failed", f"{type(e).__name__}: {e}"
            (run_dir / "error.txt").write_text(traceback.format_exc())
        finally:
            ctx.counts["llm_calls"] = ctx.llm.calls
            conn.execute("UPDATE runs SET status = ?, finished_at = ?, counts_json = ?, cost_json = ?, "
                         "failure_reason = ? WHERE run_id = ?",
                         (status, now_iso(), json.dumps(ctx.counts), json.dumps(ctx.budget.summary()),
                          reason, run_id))
            from gtm import report
            report.write_run_report(ctx, status, reason)
        return {"run_id": run_id, "mode": mode, "status": status, "reason": reason, "attempt": attempt,
                "resumed": bool(prev), "counts": ctx.counts, "notes": ctx.notes,
                "report": str(run_dir / "report.md")}


def _run_step(ctx: Ctx, step: str, fn) -> None:
    ctx.mark(step, "running")
    try:
        fn(ctx)
    except (BudgetExceeded, llm.CallCapReached, llm.UsageLimit):
        ctx.mark(step, "failed")
        raise
    except Exception:
        ctx.mark(step, "failed")
        if step == "sync":
            ctx.sync_ok = False
            ctx.note("reply sync failed: no sends this run")
            return  # the pipeline can still prospect; drain is skipped
        raise
    ctx.mark(step, "done")
