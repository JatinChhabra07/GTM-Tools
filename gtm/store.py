"""Per-product state database (plan §3). One file per product: cross-product queries are impossible."""

from __future__ import annotations

import sqlite3

from gtm import db
from gtm.paths import state_db

# Append-only. Never edit a shipped entry; add a new one.
STATE_MIGRATIONS: list[str] = [
    # v1: the full §3 data contract
    """
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        mode TEXT NOT NULL CHECK (mode IN ('full','refresh','sync','noop')),
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL CHECK (status IN ('running','done','partial','failed')),
        attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt BETWEEN 1 AND 2),
        counts_json TEXT NOT NULL DEFAULT '{}',
        cost_json TEXT NOT NULL DEFAULT '{}',
        failure_reason TEXT
    );
    CREATE TABLE steps (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        step TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending','running','done','failed')),
        external_ref TEXT,
        started_at TEXT,
        finished_at TEXT,
        PRIMARY KEY (run_id, step)
    );

    CREATE TABLE signal_codes (
        code TEXT PRIMARY KEY CHECK (code GLOB '[A-Z]*' AND code NOT GLOB '*[^A-Z0-9_]*'),
        status TEXT NOT NULL CHECK (status IN ('proposed','confirmed','rejected')),
        kind TEXT NOT NULL CHECK (kind IN ('intent','trigger','firmographic','contact','health')),
        system INTEGER NOT NULL DEFAULT 0 CHECK (system IN (0,1)),
        cost_class TEXT NOT NULL CHECK (cost_class IN ('free','paid')),
        cost_note TEXT NOT NULL DEFAULT '',
        meaning TEXT NOT NULL DEFAULT '',
        value_type TEXT NOT NULL CHECK (value_type IN ('bool','num','text')),
        threshold_op TEXT CHECK (threshold_op IN ('>','>=','<','<=','=','!=')),
        threshold_value REAL,
        min_confidence REAL NOT NULL DEFAULT 0.5 CHECK (min_confidence BETWEEN 0 AND 1),
        freshness_days INTEGER,
        rank INTEGER NOT NULL DEFAULT 100,
        CHECK (system = 0 OR (status = 'confirmed' AND kind IN ('contact','health')))
    );
    -- contact/health codes can never be rejected: failures must stay visible
    CREATE TRIGGER signal_codes_system_locked
    BEFORE UPDATE OF status ON signal_codes
    WHEN OLD.system = 1 AND NEW.status != 'confirmed'
    BEGIN
        SELECT RAISE(ABORT, 'system signal codes cannot be rejected');
    END;

    CREATE TABLE observations (
        id INTEGER PRIMARY KEY,
        account_domain TEXT NOT NULL,
        lead_key TEXT,
        collector TEXT NOT NULL,
        signal_code TEXT NOT NULL REFERENCES signal_codes(code),
        value_num REAL,
        value_text TEXT,
        confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
        evidence_url TEXT NOT NULL CHECK (evidence_url GLOB 'http*://*'),
        observed_at TEXT NOT NULL,
        run_id TEXT
    );
    CREATE INDEX observations_latest ON observations (account_domain, signal_code, observed_at);
    CREATE INDEX observations_lead ON observations (lead_key);
    -- governed vocabulary (from Harvey) + our addition: the code must be confirmed for this product
    CREATE TRIGGER observations_vocabulary
    BEFORE INSERT ON observations
    BEGIN
        SELECT RAISE(ABORT, 'unknown signal_code')
        WHERE NOT EXISTS (SELECT 1 FROM signal_codes WHERE code = NEW.signal_code);
        SELECT RAISE(ABORT, 'signal_code not confirmed for this product')
        WHERE NOT EXISTS (SELECT 1 FROM signal_codes
                          WHERE code = NEW.signal_code AND status = 'confirmed');
    END;
    -- facts are append-only; erasure deletes rows, nothing rewrites them
    CREATE TRIGGER observations_append_only
    BEFORE UPDATE ON observations
    BEGIN
        SELECT RAISE(ABORT, 'observations are append-only');
    END;

    CREATE TABLE posts (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        url TEXT NOT NULL,
        posted_at TEXT,
        author_name TEXT,
        author_handle TEXT,
        author_title TEXT,
        author_employer_evidence TEXT,
        text_excerpt TEXT NOT NULL CHECK (length(text_excerpt) <= 600),
        resolvability TEXT NOT NULL CHECK (resolvability IN ('named_person','company_event','anonymous')),
        matched_keywords_json TEXT NOT NULL DEFAULT '[]',
        run_id TEXT
    );

    CREATE TABLE accounts (
        domain TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        industry TEXT,
        size_band TEXT,
        hq_country TEXT,
        linkedin_url TEXT,
        source TEXT NOT NULL CHECK (source IN ('tal','signal')),
        tal_tier INTEGER,
        resolution_evidence TEXT NOT NULL CHECK (resolution_evidence GLOB 'http*://*'),
        lookalike_score REAL,
        prescore REAL CHECK (prescore BETWEEN 0 AND 100),
        profile_checked_at TEXT,
        status TEXT NOT NULL DEFAULT 'new'
    );

    CREATE TABLE leads (
        lead_key TEXT PRIMARY KEY,
        account_domain TEXT NOT NULL REFERENCES accounts(domain),
        full_name TEXT NOT NULL,
        title TEXT,
        persona TEXT,
        persona_match TEXT CHECK (persona_match IN ('exact','fuzzy','llm')),
        seniority TEXT,
        linkedin_url TEXT,
        email TEXT UNIQUE,
        email_source TEXT CHECK (email_source IN ('pattern','snov_finder','site_printed','generic_inbox')),
        smtp_status TEXT,
        unknown_status_reason TEXT,
        email_class TEXT CHECK (email_class IN ('valid','catchall','invalid','unknown')),
        is_generic_inbox INTEGER NOT NULL DEFAULT 0,
        is_signal_author INTEGER NOT NULL DEFAULT 0,
        referral INTEGER NOT NULL DEFAULT 0,
        source_observation_ids_json TEXT NOT NULL DEFAULT '[]',
        cost_usd REAL NOT NULL DEFAULT 0,
        CHECK (email IS NULL OR email = lower(email))
    );

    CREATE TABLE scores (
        lead_key TEXT NOT NULL REFERENCES leads(lead_key),
        run_id TEXT NOT NULL,
        total REAL NOT NULL CHECK (total BETWEEN 0 AND 100),
        dims_json TEXT NOT NULL,
        gates_json TEXT NOT NULL,
        reviewer_findings_json TEXT NOT NULL DEFAULT '[]',
        verdict TEXT NOT NULL CHECK (verdict IN ('keep','drop','needs_rework')),
        drop_reason TEXT,
        holdout INTEGER NOT NULL DEFAULT 0,
        feature_snapshot_json TEXT NOT NULL,
        PRIMARY KEY (lead_key, run_id)
    );

    CREATE TABLE sequences (
        seq_id TEXT PRIMARY KEY,
        lead_key TEXT NOT NULL REFERENCES leads(lead_key),
        steps_json TEXT NOT NULL,
        facts_used_json TEXT NOT NULL DEFAULT '[]',
        reference_policy TEXT NOT NULL
            CHECK (reference_policy IN ('self_public_professional','company_event','theme_only')),
        proof_ids_json TEXT NOT NULL DEFAULT '[]',
        recipient_email TEXT NOT NULL,
        mailbox_id TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('draft','pending_review','approved','rejected',
                                               'claimed','pushed','failed_maybe_sent','cancelled')),
        writer_attempts INTEGER NOT NULL DEFAULT 0,
        feedback_notes_json TEXT NOT NULL DEFAULT '[]',
        claimed_at TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX sequences_one_live_per_lead ON sequences (lead_key)
        WHERE status NOT IN ('rejected','cancelled');

    -- Approvals are only valid with an Ed25519 signature (gtm/signing.py). Anything can INSERT here;
    -- nothing can forge a signature without the PIN-encrypted key, and gtm run verifies every one.
    CREATE TABLE approvals (
        id INTEGER PRIMARY KEY,
        target TEXT NOT NULL CHECK (target IN ('sequence','reply_draft')),
        target_id TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        approved_by TEXT NOT NULL,
        approved_at TEXT NOT NULL,
        via TEXT NOT NULL CHECK (via IN ('tty_pin','policy')),
        edited INTEGER NOT NULL DEFAULT 0,
        signature TEXT
    );
    CREATE INDEX approvals_target ON approvals (target, target_id);

    CREATE TABLE replies (
        id TEXT PRIMARY KEY,
        seq_id TEXT NOT NULL REFERENCES sequences(seq_id),
        received_at TEXT NOT NULL,
        body_stripped TEXT NOT NULL,
        intent TEXT,
        keyword_override TEXT,
        status TEXT NOT NULL CHECK (status IN ('new','handled','needs_human'))
    );
    CREATE TABLE reply_drafts (
        id TEXT PRIMARY KEY,
        reply_id TEXT NOT NULL REFERENCES replies(id),
        recipient_email TEXT NOT NULL,
        mailbox_id TEXT NOT NULL,
        body TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending_review','approved','rejected','claimed',
                                               'sent_api','sent_manual','failed_maybe_sent')),
        updated_at TEXT NOT NULL
    );

    CREATE TABLE usage_events (
        id INTEGER PRIMARY KEY,
        run_id TEXT,
        prompt_id TEXT NOT NULL,
        model TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cache_read_tokens INTEGER,
        duration_ms INTEGER,
        outcome TEXT NOT NULL CHECK (outcome IN ('ok','invalid_output','usage_limit','error')),
        at TEXT NOT NULL
    );

    CREATE TABLE email_patterns (
        domain TEXT PRIMARY KEY,
        pattern TEXT NOT NULL,
        confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
        source TEXT NOT NULL,
        learned_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE TABLE clients (
        domain TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        contract_size REAL,
        retention_months REAL,
        notes TEXT
    );
    CREATE TABLE watermarks (
        source TEXT PRIMARY KEY,
        last_collected_at TEXT NOT NULL
    );
    """,
    # v2: pipeline state (built in the full build): resolution, snapshots, caches, review notes
    """
    ALTER TABLE accounts ADD COLUMN created_at TEXT;
    ALTER TABLE accounts ADD COLUMN linkedin_company_checked_at TEXT;
    ALTER TABLE leads ADD COLUMN first_name TEXT;
    ALTER TABLE leads ADD COLUMN country TEXT;
    ALTER TABLE leads ADD COLUMN status TEXT NOT NULL DEFAULT 'new';
    ALTER TABLE leads ADD COLUMN created_at TEXT;
    ALTER TABLE leads ADD COLUMN enriched_at TEXT;
    ALTER TABLE posts ADD COLUMN intent_code TEXT;
    ALTER TABLE posts ADD COLUMN intent_strength INTEGER;
    ALTER TABLE posts ADD COLUMN employer TEXT;
    ALTER TABLE posts ADD COLUMN account_domain TEXT;
    ALTER TABLE posts ADD COLUMN lead_key TEXT;
    ALTER TABLE posts ADD COLUMN labelled_at TEXT;
    ALTER TABLE scores ADD COLUMN created_at TEXT;
    ALTER TABLE sequences ADD COLUMN created_at TEXT;
    ALTER TABLE sequences ADD COLUMN pushed_at TEXT;
    ALTER TABLE sequences ADD COLUMN score REAL;
    ALTER TABLE sequences ADD COLUMN holdout INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE sequences ADD COLUMN validator_json TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE replies ADD COLUMN lead_key TEXT;
    ALTER TABLE replies ADD COLUMN classifier_json TEXT;
    ALTER TABLE reply_drafts ADD COLUMN created_at TEXT;

    -- untrusted page text kept ONLY to diff the next visit (SITE_CHANGE / PRICING_CHANGE)
    CREATE TABLE page_snapshots (
        url TEXT PRIMARY KEY,
        account_domain TEXT NOT NULL,
        kind TEXT NOT NULL,
        text_hash TEXT NOT NULL,
        text TEXT NOT NULL,
        fetched_at TEXT NOT NULL
    );
    CREATE TABLE persona_cache (
        title_norm TEXT PRIMARY KEY,
        persona TEXT,
        at TEXT NOT NULL
    );
    -- founder feedback from `gtm review`: campaign-wide vs one contact (scoped separately, plan Phase 7)
    CREATE TABLE writer_notes (
        id INTEGER PRIMARY KEY,
        scope TEXT NOT NULL CHECK (scope IN ('campaign','contact')),
        lead_key TEXT,
        note TEXT NOT NULL,
        at TEXT NOT NULL
    );
    CREATE TABLE golden_labels (
        post_id TEXT NOT NULL,
        code TEXT NOT NULL,
        label INTEGER NOT NULL CHECK (label IN (0,1)),
        PRIMARY KEY (post_id, code)
    );
    CREATE TABLE proposals (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        body_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','accepted','rejected')),
        at TEXT NOT NULL
    );
    """,
    # v3: what the validator needs at push time is frozen at write time (proofs, allowed URLs)
    """
    ALTER TABLE sequences ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE sequences ADD COLUMN edited INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE sequences ADD COLUMN notice TEXT;
    """,
]


def open_state(slug: str) -> sqlite3.Connection:
    conn = db.connect(state_db(slug))
    db.migrate(conn, STATE_MIGRATIONS)
    return conn
