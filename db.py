"""Database layer: SQLite (default) or hosted Postgres (DATABASE_URL).

Local/dev writes a SQLite file at SQLITE_PATH (or ./leads.db). A hosted deploy
(e.g. Vercel + Neon / Vercel Postgres / Supabase) sets
DATABASE_URL=postgres://... and every module routes through here instead of
opening sqlite3 directly.

The rest of the app is written in ONE SQL dialect — SQLite-flavoured, with '?'
placeholders. When DATABASE_URL points at Postgres, connect() returns a thin
wrapper that translates each statement (placeholders + a few DDL tokens) to
psycopg. Callers never branch on the backend; a handful of helpers here
(table_columns, insert_returning_id, sync_serial) cover the few operations that
genuinely differ between the two engines.

Why this shape: the existing modules (storage/models/config/billing/ff_account/
waitlist) each open their own connection and CREATE TABLE IF NOT EXISTS lazily.
Keeping that pattern — but pointing it at db.connect() — means the Postgres path
is a small, centralized surface rather than a rewrite of every query.
"""
import os
import re
import sqlite3
from pathlib import Path

# Default local file. On a read-only serverless FS this path is never opened as
# long as DATABASE_URL is set (Postgres), so importing this module is safe there
# — it only builds a Path object, it does not touch the filesystem.
DB_PATH = Path(os.getenv("SQLITE_PATH") or (Path(__file__).parent / "leads.db"))


def database_url() -> str:
    return (os.getenv("DATABASE_URL") or "").strip()


def is_postgres() -> bool:
    """True when DATABASE_URL points at a Postgres instance."""
    u = database_url().lower()
    return u.startswith("postgres://") or u.startswith("postgresql://")


def backend() -> str:
    return "postgres" if is_postgres() else "sqlite"


# --- SQLite dialect -> Postgres translation --------------------------------
# The app writes SQLite-flavoured DDL/DML; these substitutions make it valid
# Postgres. Kept deliberately small: only the tokens this codebase actually uses.
_DDL_SUBS = (
    # INTEGER PRIMARY KEY AUTOINCREMENT -> SERIAL PRIMARY KEY
    (re.compile(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", re.I), "SERIAL PRIMARY KEY"),
    # Postgres has no per-column COLLATE NOCASE; the app lowercases emails
    # consistently before write/lookup, so dropping it preserves behavior.
    (re.compile(r"\s+COLLATE\s+NOCASE", re.I), ""),
)


def _to_pg(sql: str) -> str:
    for pat, repl in _DDL_SUBS:
        sql = pat.sub(repl, sql)
    # psycopg uses pyformat ('%s'); a literal '%' must be doubled first. No
    # statement in this codebase uses a literal '%', but keep it correct.
    sql = sql.replace("%", "%%")
    sql = sql.replace("?", "%s")
    return sql


class Conn:
    """Uniform connection wrapper over sqlite3 / psycopg.

    Mirrors the sqlite3.Connection surface the app relies on: .execute() returns
    a cursor supporting fetchone/fetchall/iteration; .executescript() runs a
    multi-statement script. Used as a context manager it commits on success,
    rolls back on error, and always closes (important for Postgres connection
    limits — sqlite3's own context manager never closed, which leaked cheaply).
    """

    def __init__(self, raw, pg: bool):
        self._raw = raw
        self.pg = pg

    def execute(self, sql: str, params=()):
        if self.pg:
            cur = self._raw.cursor()
            cur.execute(_to_pg(sql), params)
            return cur
        return self._raw.execute(sql, params)

    def executescript(self, script: str):
        if self.pg:
            with self._raw.cursor() as cur:
                cur.execute(_to_pg(script))
        else:
            self._raw.executescript(script)

    @property
    def raw(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._raw.commit()
            else:
                self._raw.rollback()
        finally:
            self._raw.close()
        return False


def connect() -> Conn:
    """Open a connection to the configured backend (Postgres or SQLite)."""
    if is_postgres():
        import psycopg  # lazy: local/dev never needs psycopg installed

        raw = psycopg.connect(database_url())
        return Conn(raw, pg=True)
    raw = sqlite3.connect(DB_PATH)
    return Conn(raw, pg=False)


# --- Cross-dialect helpers -------------------------------------------------
def table_columns(conn: Conn, table: str) -> set[str]:
    """Column names for a table — replaces `PRAGMA table_info(...)`.

    Used by the idempotent ADD COLUMN migrations so they work on both engines.
    """
    if conn.pg:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=?",
            (table,),
        ).fetchall()
        return {r[0] for r in rows}
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def insert_returning_id(conn: Conn, sql: str, params, id_col: str = "id"):
    """INSERT and return the new autoincrement id.

    Postgres has no cursor.lastrowid, so append RETURNING; SQLite uses lastrowid.
    """
    if conn.pg:
        cur = conn.execute(f"{sql} RETURNING {id_col}", params)
        return cur.fetchone()[0]
    return conn.execute(sql, params).lastrowid


def sync_serial(conn: Conn, table: str, col: str = "id") -> None:
    """Advance Postgres' identity sequence past the current MAX(id).

    Needed after an explicit-id insert (the operator row is forced to id=1):
    SERIAL/identity sequences don't observe explicit inserts, so the next
    default insert would otherwise collide. No-op on SQLite, where AUTOINCREMENT
    already tracks the max.
    """
    if not conn.pg:
        return
    conn.execute(
        f"SELECT setval(pg_get_serial_sequence(?, ?), "
        f"(SELECT COALESCE(MAX({col}), 1) FROM {table}))",
        (table, col),
    )
