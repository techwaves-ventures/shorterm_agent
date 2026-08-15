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

Schema creation goes through open_with_schema() rather than running the DDL on
the caller's own connection. On SQLite the lazy-every-time behavior is kept
exactly; on Postgres running DDL inside the caller's transaction deadlocks two
concurrent writers. See open_with_schema for the full reasoning.
"""
import os
import re
import sqlite3
import threading
import zlib
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


# --- Schema creation -------------------------------------------------------
# Which (dsn, key) schemas this *process* has already created on Postgres.
# Keyed by DSN too, so repointing DATABASE_URL mid-process re-runs the DDL.
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_DONE: set[tuple[str, str]] = set()


def _reset_schema_state() -> None:
    """Forget the per-process schema latch — for use in a forked child.

    The latch is a claim about *this* process ("I already ran the DDL on this
    connection's database"). fork() copies it into a child that has run nothing,
    so without this a forked worker skips a pending ADD COLUMN migration and
    then fails on the missing column. The lock is rebuilt rather than reused
    because a lock held by another thread at fork time stays locked forever in
    the child.
    """
    global _SCHEMA_LOCK, _SCHEMA_DONE
    _SCHEMA_LOCK = threading.Lock()
    _SCHEMA_DONE = set()


if hasattr(os, "register_at_fork"):  # not available on Windows
    os.register_at_fork(after_in_child=_reset_schema_state)


def _advisory_key(key: str) -> int:
    """Stable 63-bit advisory-lock id for a schema key.

    crc32 rather than hash(): hash() is salted per process, so two processes
    would take *different* locks and not serialise against each other at all.
    """
    return (0x5645_4E31 << 32) | zlib.crc32(key.encode())


def _ensure_pg_schema(key: str, ddl) -> None:
    """Run `ddl` once per process, in its own committed transaction."""
    dsn = database_url()
    with _SCHEMA_LOCK:
        if (dsn, key) in _SCHEMA_DONE:
            return
        lock_id = _advisory_key(key)
        with connect() as c:
            # Session-level, NOT pg_advisory_xact_lock: an xact lock is a silent
            # no-op under autocommit, and this connection may become an
            # autocommit/pooled one. Serialises first creation across processes,
            # which is what stops concurrent CREATE TABLE IF NOT EXISTS from
            # racing into Postgres' own catalog (duplicate pg_type/pg_class).
            c.execute("SELECT pg_advisory_lock(?)", (lock_id,))
            try:
                ddl(c)
            finally:
                # Must be explicit. A session lock outlives the transaction, so
                # if this connection is ever pool-backed rather than closed, a
                # missed unlock would block first-creation for every other
                # process for the life of that session.
                c.execute("SELECT pg_advisory_unlock(?)", (lock_id,))
        _SCHEMA_DONE.add((dsn, key))


def open_with_schema(key: str, ddl, session=None) -> Conn:
    """Open a connection whose tables are guaranteed to exist.

    `ddl(conn)` creates/migrates this module's tables; `key` names them for the
    once-per-process latch. `session(conn)` applies per-connection session state
    (e.g. PRAGMA foreign_keys) and therefore runs on the returned connection
    every time, on both backends.

    Postgres: the DDL runs once per process on a *separate*, committed
    connection, and the caller gets a clean one. It must not ride in the
    caller's transaction — CREATE INDEX IF NOT EXISTS takes a ShareLock on the
    table even when the index already exists, and holds it to commit. Two
    writers both take that self-compatible lock, then each blocks upgrading to
    the RowExclusiveLock its INSERT needs: a lock-upgrade deadlock that kills a
    drainer mid-pass. A pending ALTER is worse than a deadlock — a nested
    connection opened underneath it waits on AccessExclusiveLock behind a
    transaction that is idle waiting on its own client, so there is no cycle for
    Postgres to detect and the process hangs at boot forever.

    SQLite: `ddl` runs on the returned connection every time, exactly as before.
    This is deliberate and load-bearing, not an oversight — the test fixtures
    repoint DB_PATH per test and rely on the schema being created lazily on
    first use, so a once-per-process latch here hands a later test an empty
    database.
    """
    if not is_postgres():
        c = connect()
        if session is not None:
            session(c)
        ddl(c)
        return c
    _ensure_pg_schema(key, ddl)
    c = connect()
    if session is not None:
        session(c)
    return c


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
