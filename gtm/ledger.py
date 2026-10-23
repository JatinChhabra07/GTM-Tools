"""Global contact ledger (plan §2.3). The ONE deliberate cross-product store: compliance and
sending capacity only, never knowledge or lead content. Written only by gtm; multi-row
changes use BEGIN IMMEDIATE so two products can't race each other."""

from __future__ import annotations

import hashlib
import sqlite3

from gtm import db
from gtm.paths import ledger_db

LEDGER_MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE suppression (
        id INTEGER PRIMARY KEY,
        email_hash TEXT,
        domain TEXT,
        linkedin_hash TEXT,
        reason TEXT NOT NULL CHECK (reason IN ('unsubscribe','bounce','complaint','erasure','manual')),
        scope TEXT NOT NULL DEFAULT 'global',
        added_at TEXT NOT NULL,
        CHECK (email_hash IS NOT NULL OR domain IS NOT NULL OR linkedin_hash IS NOT NULL)
    );
    CREATE INDEX suppression_email ON suppression (email_hash);
    CREATE INDEX suppression_domain ON suppression (domain);
    CREATE INDEX suppression_linkedin ON suppression (linkedin_hash);

    CREATE TABLE account_sequences (
        id TEXT PRIMARY KEY,
        product TEXT NOT NULL,
        lead_key TEXT NOT NULL,
        email_hash TEXT NOT NULL,
        domain TEXT NOT NULL,
        mailbox_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('active','finished','replied','opted_out','bounced','stopped')),
        step_sent INTEGER NOT NULL DEFAULT 0,
        referral INTEGER NOT NULL DEFAULT 0,
        started_at TEXT NOT NULL,
        ended_at TEXT
    );
    CREATE INDEX account_sequences_domain ON account_sequences (domain, status);

    CREATE TABLE contact_log (
        id INTEGER PRIMARY KEY,
        email_hash TEXT NOT NULL,
        domain TEXT NOT NULL,
        product TEXT NOT NULL,
        mailbox_id TEXT NOT NULL,
        step INTEGER NOT NULL,
        email_class TEXT NOT NULL,
        sent_at TEXT NOT NULL
    );

    CREATE TABLE mailboxes (
        id TEXT PRIMARY KEY,
        address TEXT NOT NULL UNIQUE,
        daily_cap INTEGER NOT NULL CHECK (daily_cap BETWEEN 1 AND 200),
        warmup_start TEXT,
        warmup_min_days INTEGER NOT NULL DEFAULT 14,
        start_sends INTEGER NOT NULL DEFAULT 5,
        daily_increase INTEGER NOT NULL DEFAULT 3,
        ready_state TEXT NOT NULL DEFAULT 'unknown' CHECK (ready_state IN ('unknown','ready','not_ready')),
        ready_checked_at TEXT,
        bounce_rate_7d REAL NOT NULL DEFAULT 0,
        paused INTEGER NOT NULL DEFAULT 0,
        catchall_allowed INTEGER NOT NULL DEFAULT 0,
        signature TEXT
    );
    -- capacity is counted in touches (emails) per mailbox per day, reserved at claim time
    CREATE TABLE mailbox_sends (
        mailbox_id TEXT NOT NULL REFERENCES mailboxes(id),
        date TEXT NOT NULL,
        touches INTEGER NOT NULL DEFAULT 0 CHECK (touches >= 0),
        PRIMARY KEY (mailbox_id, date)
    );

    CREATE TABLE sending_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        paused INTEGER NOT NULL DEFAULT 0,
        reason TEXT,
        at TEXT,
        resume_signature TEXT
    );
    INSERT INTO sending_state (id, paused) VALUES (1, 0);

    CREATE TABLE autosend (
        product TEXT PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 0,
        enabled_at TEXT,
        signature TEXT
    );

    CREATE TABLE erasure_log (
        id INTEGER PRIMARY KEY,
        email_hash TEXT NOT NULL,
        at TEXT NOT NULL,
        scopes_cleared_json TEXT NOT NULL
    );
    """,
    # v2: Snov wiring, touch reservations per sequence, manual actions, bounces
    """
    CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    -- one Snov list + campaign per (product, mailbox): the Snov sender really is the mailbox
    CREATE TABLE snov_campaigns (
        product TEXT NOT NULL,
        mailbox_id TEXT NOT NULL REFERENCES mailboxes(id),
        list_id INTEGER NOT NULL,
        campaign_id INTEGER NOT NULL,
        settings_ok INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        PRIMARY KEY (product, mailbox_id)
    );
    -- exactly what a push reserved, so a failed push before sending can be released precisely
    CREATE TABLE reservations (
        seq_id TEXT NOT NULL,
        mailbox_id TEXT NOT NULL,
        date TEXT NOT NULL,
        touches INTEGER NOT NULL,
        PRIMARY KEY (seq_id, date)
    );
    CREATE TABLE bounces (
        id INTEGER PRIMARY KEY,
        mailbox_id TEXT NOT NULL,
        email_hash TEXT NOT NULL,
        at TEXT NOT NULL,
        UNIQUE (mailbox_id, email_hash)
    );
    -- things the API can't do (delete a Snov prospect, send a reply): shown in every report until done
    CREATE TABLE manual_actions (
        id INTEGER PRIMARY KEY,
        product TEXT,
        action TEXT NOT NULL,
        detail TEXT NOT NULL,
        created_at TEXT NOT NULL,
        done_at TEXT
    );
    ALTER TABLE account_sequences ADD COLUMN email_class TEXT;
    """,
]


def open_ledger() -> sqlite3.Connection:
    conn = db.connect(ledger_db())
    db.migrate(conn, LEDGER_MIGRATIONS)
    return conn


def email_hash(email: str) -> str:
    """Suppression stores hashes, so the ledger never holds a readable address."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def is_suppressed(conn: sqlite3.Connection, *, email: str | None = None,
                  domain: str | None = None, linkedin_url: str | None = None) -> bool:
    clauses, args = [], []
    if email:
        clauses.append("email_hash = ?")
        args.append(email_hash(email))
    if domain:
        clauses.append("domain = ?")
        args.append(domain.lower())
    if linkedin_url:
        clauses.append("linkedin_hash = ?")
        args.append(email_hash(linkedin_url.rstrip("/").lower()))
    if not clauses:
        return False
    row = conn.execute(f"SELECT 1 FROM suppression WHERE {' OR '.join(clauses)} LIMIT 1", args).fetchone()
    return row is not None


def sending_paused(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute("SELECT paused FROM sending_state WHERE id = 1").fetchone()[0])
