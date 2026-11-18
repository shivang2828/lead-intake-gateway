"""Database helpers shared by the API (main.py) and the worker (worker.py).

Postgres when DATABASE_URL is set (production), SQLite file otherwise (local dev and tests, zero setup).
Only three things differ between them, and all three live in this file:
placeholders (? vs %s), the auto-increment id column, and row locking for the worker's claim.
"""
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

DATABASE_URL = os.environ.get("DATABASE_URL")
DB_PATH = os.environ.get("DB_PATH", "leads.db")
IS_POSTGRES = bool(DATABASE_URL)

# Postgres: lock the row we pick and skip rows other workers already locked, so workers never wait on each other.
# SQLite has no row locks; it runs one write at a time, so the claim is already safe there.
SKIP_LOCKED = " FOR UPDATE SKIP LOCKED" if IS_POSTGRES else ""

if IS_POSTGRES:
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    # A pool keeps a few connections open: opening a new one to a cloud DB costs ~100ms (network + TLS).
    _pool = ConnectionPool(
        DATABASE_URL, min_size=1, max_size=5, open=True,
        kwargs={"row_factory": dict_row},
        check=ConnectionPool.check_connection,  # cloud DBs drop idle connections; test before handing one out
    )


def run_sql(sql: str, params: tuple = ()) -> list[dict]:
    """Run one statement in its own transaction (commit on success). Returns any rows as dicts.
    SQL is written with '?' placeholders; values are always passed separately, never pasted into the string."""
    if IS_POSTGRES:
        with _pool.connection() as conn:  # commits when the block ends, rolls back on error
            cur = conn.execute(sql.replace("?", "%s"), params)
            return cur.fetchall() if cur.description else []

    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """One fixed text format for every timestamp, so string comparison in SQL = time comparison."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def init_db():
    id_column = "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    # shortcut: timestamps are ISO text in both DBs to keep one code path; in a Postgres-only app use TIMESTAMPTZ.
    run_sql(f"""
        CREATE TABLE IF NOT EXISTS leads (
            id              {id_column},
            name            TEXT NOT NULL,
            phone           TEXT NOT NULL,
            source          TEXT NOT NULL,
            campaign        TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'queued',
                -- queued / scheduled -> calling -> called -> syncing -> synced
                --                                   (sync fails) -> sync_retry -> ... -> failed (dead letter)
            outcome         TEXT,                            -- result of the call
            next_attempt_at TEXT NOT NULL,                   -- worker won't touch the lead before this time
            sync_attempts   INTEGER NOT NULL DEFAULT 0,      -- how many times we tried to push to the CRM
            last_error      TEXT,                            -- why the last CRM push failed
            created_at      TEXT NOT NULL
        )
    """)
    # The database itself refuses a second lead with the same phone in the same campaign,
    # so duplicates are blocked even if two requests arrive at the same moment.
    run_sql("CREATE UNIQUE INDEX IF NOT EXISTS uniq_campaign_phone ON leads (campaign, phone)")
    # The worker asks "which leads are due?" every second; this index answers without scanning the whole table.
    run_sql("CREATE INDEX IF NOT EXISTS idx_due ON leads (status, next_attempt_at)")
