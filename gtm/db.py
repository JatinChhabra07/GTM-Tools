"""Shared SQLite plumbing: connection settings + append-only migrations via PRAGMA user_version."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from gtm.paths import ensure_private_dir


def connect(path: Path) -> sqlite3.Connection:
    ensure_private_dir(path.parent)
    # isolation_level=None: we issue BEGIN/COMMIT ourselves (BEGIN IMMEDIATE where it matters)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection, migrations: Sequence[str]) -> int:
    """Apply migrations[user_version:]. The list is append-only: never edit a shipped entry."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > len(migrations):
        raise RuntimeError(
            f"database is at schema v{current}, code only knows v{len(migrations)}: update the code"
        )
    for version, sql in enumerate(migrations[current:], start=current + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            # statement by statement: executescript() would COMMIT and break the transaction
            for stmt in _split(sql):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version={version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return len(migrations)


def _split(sql: str) -> list[str]:
    """Split a migration into statements, keeping CREATE TRIGGER ... END blocks whole."""
    out: list[str] = []
    buf: list[str] = []
    in_trigger = False
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        upper = stripped.upper()
        if upper.startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if upper == "END;":
                out.append("\n".join(buf))
                buf, in_trigger = [], False
        elif stripped.endswith(";"):
            out.append("\n".join(buf))
            buf = []
    if buf:
        out.append("\n".join(buf))
    return out
