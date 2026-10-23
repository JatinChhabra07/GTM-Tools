"""`gtm forget <email>` (plan §6.1 erasure): every store, every product, then global suppression.

SQLite rows, run files redacted, KB memories by lead_key (+ mem0 history rows), Snov do-not-email +
Unsubscribed in our campaigns, and the hashes go into global suppression so the person is never
re-collected. Snov has no delete-prospect API: that one step becomes a manual action.
"""

from __future__ import annotations

import json
import os
import sqlite3

from gtm import kb, ledger, store
from gtm.paths import product_data_dir, products_dir, runs_dir
from gtm.util import now_iso


def product_slugs() -> list[str]:
    root = products_dir()
    return sorted(p.name for p in root.iterdir() if (p / "product.yaml").exists()) if root.exists() else []


def _redact_runs(slug: str, needles: list[str]) -> int:
    n = 0
    root = runs_dir(slug)
    if not root.exists():
        return 0
    for dirpath, _, files in os.walk(root):
        for f in files:
            path = os.path.join(dirpath, f)
            try:
                text = open(path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            new = text
            for s in needles:
                if s:
                    new = new.replace(s, "[erased]")
            if new != text:
                open(path, "w", encoding="utf-8").write(new)
                n += 1
    return n


def _purge_mem0_history(slug: str, memory_ids: list[str]) -> None:
    path = product_data_dir(slug) / "mem0" / "history.db"
    if not memory_ids or not path.exists():
        return
    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "history" in tables:
            conn.executemany("DELETE FROM history WHERE memory_id = ?", [(m,) for m in memory_ids])
            conn.commit()
    finally:
        conn.close()


def forget_in_product(slug: str, email: str) -> dict:
    conn = store.open_state(slug)
    email = email.strip().lower()
    leads = conn.execute("SELECT * FROM leads WHERE email = ?", (email,)).fetchall()
    out = {"leads": len(leads), "run_files": 0, "memories": 0}
    needles = [email]
    for lead in leads:
        lk = lead["lead_key"]
        needles += [lead["full_name"], lead["linkedin_url"] or ""]
        seqs = [r[0] for r in conn.execute("SELECT seq_id FROM sequences WHERE lead_key = ?", (lk,))]
        replies = [r[0] for r in conn.execute("SELECT id FROM replies WHERE lead_key = ? OR seq_id IN (%s)"
                                              % ",".join("?" * len(seqs)), (lk, *seqs))] if seqs else []
        conn.execute("BEGIN IMMEDIATE")
        try:
            if replies:
                q = ",".join("?" * len(replies))
                drafts = [r[0] for r in conn.execute(f"SELECT id FROM reply_drafts WHERE reply_id IN ({q})", replies)]
                if drafts:
                    conn.execute(f"DELETE FROM approvals WHERE target = 'reply_draft' AND target_id IN ({','.join('?' * len(drafts))})", drafts)
                conn.execute(f"DELETE FROM reply_drafts WHERE reply_id IN ({q})", replies)
                conn.execute(f"DELETE FROM replies WHERE id IN ({q})", replies)
            if seqs:
                q = ",".join("?" * len(seqs))
                conn.execute(f"DELETE FROM approvals WHERE target = 'sequence' AND target_id IN ({q})", seqs)
                conn.execute(f"DELETE FROM sequences WHERE seq_id IN ({q})", seqs)
            conn.execute("DELETE FROM scores WHERE lead_key = ?", (lk,))
            conn.execute("DELETE FROM observations WHERE lead_key = ?", (lk,))
            conn.execute("DELETE FROM posts WHERE lead_key = ?", (lk,))
            conn.execute("DELETE FROM writer_notes WHERE lead_key = ?", (lk,))
            conn.execute("DELETE FROM leads WHERE lead_key = ?", (lk,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        try:
            rows = kb.backend(slug).all({"lead_key": lk})
            _purge_mem0_history(slug, [r["id"] for r in rows])
            out["memories"] += kb.forget_lead(slug, lk)
        except kb.KBError:
            pass
    out["run_files"] = _redact_runs(slug, [n for n in needles if n])
    return out


def forget(email: str) -> dict:
    email = email.strip().lower()
    led = ledger.open_ledger()
    result = {"products": {}}
    linkedin = []
    for slug in product_slugs():
        conn = store.open_state(slug)
        linkedin += [r[0] for r in conn.execute("SELECT linkedin_url FROM leads WHERE email = ? AND linkedin_url IS NOT NULL", (email,))]
        result["products"][slug] = forget_in_product(slug, email)
    h = ledger.email_hash(email)
    led.execute("BEGIN IMMEDIATE")
    try:
        led.execute("INSERT INTO suppression (email_hash, reason, added_at) VALUES (?, 'erasure', ?)", (h, now_iso()))
        for url in linkedin:
            led.execute("INSERT INTO suppression (linkedin_hash, reason, added_at) VALUES (?, 'erasure', ?)",
                        (ledger.email_hash(url.rstrip("/").lower()), now_iso()))
        led.execute("INSERT INTO erasure_log (email_hash, at, scopes_cleared_json) VALUES (?,?,?)",
                    (h, now_iso(), json.dumps(result)))
        led.execute("COMMIT")
    except Exception:
        led.execute("ROLLBACK")
        raise
    try:
        from gtm.handler import stop_everywhere
        from gtm.sources import snov
        if led.execute("SELECT 1 FROM snov_campaigns LIMIT 1").fetchone():
            stop_everywhere(led, snov.client(), email)
            led.execute("INSERT INTO manual_actions (action, detail, created_at) VALUES ('snov_delete', ?, ?)",
                        (f"delete prospect {h[:12]}... from every Snov list by hand (no API for deleting)", now_iso()))
            result["snov"] = "blocked; manual delete queued"
    except Exception as e:
        result["snov"] = f"not reached ({type(e).__name__}): queued as manual action"
        led.execute("INSERT INTO manual_actions (action, detail, created_at) VALUES ('snov_suppress', ?, ?)",
                    (f"add erased address {h[:12]}... to Snov do-not-email and delete it", now_iso()))
    return result
