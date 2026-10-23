import sqlite3

import pytest

from gtm import catalog, ledger, store
from gtm.paths import state_db


def _obs(conn, code, url="https://acme.com/careers"):
    conn.execute(
        "INSERT INTO observations (account_domain, collector, signal_code, value_text, confidence,"
        " evidence_url, observed_at) VALUES ('acme.com','test',?,'x',0.9,?,'2026-10-23')",
        (code, url),
    )


@pytest.fixture
def conn():
    c = store.open_state("acme")
    catalog.seed(c)
    return c


def test_migrations_are_idempotent(conn):
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    assert v == len(store.STATE_MIGRATIONS)
    again = store.open_state("acme")
    assert again.execute("PRAGMA user_version").fetchone()[0] == v


def test_unknown_signal_code_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError, match="unknown signal_code"):
        _obs(conn, "MADE_UP_CODE")


def test_proposed_code_is_not_collected_until_confirmed(conn):
    with pytest.raises(sqlite3.IntegrityError, match="not confirmed"):
        _obs(conn, "FUNDING")
    conn.execute("UPDATE signal_codes SET status='confirmed' WHERE code='FUNDING'")
    _obs(conn, "FUNDING")
    assert conn.execute("SELECT count(*) FROM observations").fetchone()[0] == 1


def test_rejected_code_is_refused(conn):
    conn.execute("UPDATE signal_codes SET status='rejected' WHERE code='FUNDING'")
    with pytest.raises(sqlite3.IntegrityError, match="not confirmed"):
        _obs(conn, "FUNDING")


def test_system_codes_always_on_and_not_rejectable(conn):
    _obs(conn, "SITE_UNREACHABLE")  # health code: confirmed from the start
    with pytest.raises(sqlite3.IntegrityError, match="cannot be rejected"):
        conn.execute("UPDATE signal_codes SET status='rejected' WHERE code='SITE_UNREACHABLE'")


def test_observations_are_append_only(conn):
    _obs(conn, "SITE_UNREACHABLE")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE observations SET confidence=0.1")


def test_evidence_url_required(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _obs(conn, "SITE_UNREACHABLE", url="not-a-url")


def test_seed_never_overrides_founder_choice(conn):
    conn.execute("UPDATE signal_codes SET status='rejected' WHERE code='FUNDING'")
    assert catalog.seed(conn) == 0
    assert conn.execute("SELECT status FROM signal_codes WHERE code='FUNDING'").fetchone()[0] == "rejected"


def test_products_have_separate_databases():
    a, b = store.open_state("prod-a"), store.open_state("prod-b")
    catalog.seed(a)
    a.execute("INSERT INTO clients (domain, name) VALUES ('x.com','X')")
    assert b.execute("SELECT count(*) FROM clients").fetchone()[0] == 0
    assert state_db("prod-a") != state_db("prod-b")


def test_lead_email_must_be_lowercase(conn):
    conn.execute("INSERT INTO accounts (domain,name,source,resolution_evidence) VALUES "
                 "('acme.com','Acme','tal','https://acme.com')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO leads (lead_key, account_domain, full_name, email) "
                     "VALUES ('k','acme.com','A B','A@Acme.com')")


def test_ledger_suppression_is_hashed_and_global():
    lg = ledger.open_ledger()
    lg.execute("INSERT INTO suppression (email_hash, reason, added_at) VALUES (?, 'unsubscribe', 'now')",
               (ledger.email_hash(" Jane@Acme.com "),))
    assert ledger.is_suppressed(lg, email="jane@acme.com")
    assert not ledger.is_suppressed(lg, email="john@acme.com")
    assert not lg.execute("SELECT 1 FROM suppression WHERE email_hash LIKE '%@%'").fetchone()
    assert not ledger.sending_paused(lg)
