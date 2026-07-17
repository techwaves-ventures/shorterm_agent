"""SQLite-backed dedup store. Returns only items not seen before.

The DB file location is configurable via the SQLITE_PATH env var so a hosted
deployment can point it at a mounted persistent disk (SQLite on an ephemeral
container filesystem loses data between deploys). See DEPLOY.md for the managed
Postgres migration path (DATABASE_URL) that supersedes this for scale.
"""
import ast
import json
import os
import sqlite3
from pathlib import Path
from typing import Iterable

DB_PATH = Path(os.getenv("SQLITE_PATH") or (Path(__file__).parent / "leads.db"))


def _conn():
    c = sqlite3.connect(DB_PATH)
    # Schemas below are the CURRENT (multi-tenant) shape — tenant_id is part of
    # the primary key. Fresh databases get this directly; pre-existing
    # single-user databases are migrated by _migrate_tenant_id() below.
    c.execute(
        """CREATE TABLE IF NOT EXISTS seen (
            tenant_id TEXT NOT NULL DEFAULT '1',
            site TEXT NOT NULL,
            kind TEXT NOT NULL,
            item_id TEXT NOT NULL,
            payload TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tenant_id, site, kind, item_id)
        )"""
    )
    # The responder agent's decision for each lead. One row per
    # (tenant_id, site, item_id). status: draft | skipped | sent | dismissed
    c.execute(
        """CREATE TABLE IF NOT EXISTS responses (
            tenant_id TEXT NOT NULL DEFAULT '1',
            site TEXT NOT NULL,
            kind TEXT NOT NULL,
            item_id TEXT NOT NULL,
            status TEXT NOT NULL,
            unit_id TEXT,
            reason TEXT,
            draft TEXT,
            confidence TEXT,
            tenant_email TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP,
            emailed_at TIMESTAMP,
            PRIMARY KEY (tenant_id, site, item_id)
        )"""
    )
    # Idempotent migration: add columns that older leads.db files lack. Must run
    # before the tenant_id rebuild so the copy SELECT sees these columns.
    have = {row[1] for row in c.execute("PRAGMA table_info(responses)")}
    for col, decl in (("tenant_email", "TEXT"), ("emailed_at", "TIMESTAMP")):
        if col not in have:
            c.execute(f"ALTER TABLE responses ADD COLUMN {col} {decl}")
    _migrate_tenant_id(c)
    return c


def _migrate_tenant_id(c) -> None:
    """Rebuild pre-multi-tenant tables that lack a tenant_id column.

    Adding tenant_id to the primary key can't be done with ALTER, so each legacy
    table is rebuilt and its rows copied onto the operator tenant ('1'). No-op
    once migrated (the tenant_id column is already present).
    """
    seen_cols = {row[1] for row in c.execute("PRAGMA table_info(seen)")}
    if "tenant_id" not in seen_cols:
        c.executescript(
            """
            CREATE TABLE seen_new (
                tenant_id TEXT NOT NULL DEFAULT '1',
                site TEXT NOT NULL, kind TEXT NOT NULL, item_id TEXT NOT NULL,
                payload TEXT, first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, site, kind, item_id)
            );
            INSERT INTO seen_new (tenant_id, site, kind, item_id, payload, first_seen)
                SELECT '1', site, kind, item_id, payload, first_seen FROM seen;
            DROP TABLE seen;
            ALTER TABLE seen_new RENAME TO seen;
            """
        )

    resp_cols = {row[1] for row in c.execute("PRAGMA table_info(responses)")}
    if "tenant_id" not in resp_cols:
        c.executescript(
            """
            CREATE TABLE responses_new (
                tenant_id TEXT NOT NULL DEFAULT '1',
                site TEXT NOT NULL, kind TEXT NOT NULL, item_id TEXT NOT NULL,
                status TEXT NOT NULL, unit_id TEXT, reason TEXT, draft TEXT,
                confidence TEXT, tenant_email TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_at TIMESTAMP, emailed_at TIMESTAMP,
                PRIMARY KEY (tenant_id, site, item_id)
            );
            INSERT INTO responses_new (tenant_id, site, kind, item_id, status,
                unit_id, reason, draft, confidence, tenant_email,
                created_at, sent_at, emailed_at)
                SELECT '1', site, kind, item_id, status, unit_id, reason, draft,
                    confidence, tenant_email, created_at, sent_at, emailed_at
                FROM responses;
            DROP TABLE responses;
            ALTER TABLE responses_new RENAME TO responses;
            """
        )


def _parse_payload(s: str) -> dict:
    """Payloads are stored as JSON. Older rows used Python repr (str(dict)),
    so fall back to literal_eval for those."""
    if not s:
        return {}
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        try:
            val = ast.literal_eval(s)
            return val if isinstance(val, dict) else {}
        except (ValueError, SyntaxError):
            return {}


def filter_new(tenant_id: str, site: str, kind: str, items: Iterable[dict]) -> list[dict]:
    """Record items and return only the ones not seen before (for this tenant).

    Brand-new item_ids are inserted and returned (so they get notified/drafted).
    Already-seen items are NOT returned, but their stored payload is refreshed
    when the freshly-scraped payload differs — this lets a re-scrape backfill
    richer data (e.g. the lead detail view) onto existing rows without
    re-notifying. `first_seen` is preserved so ordering stays stable.
    """
    new = []
    with _conn() as c:
        for it in items:
            iid = str(it["id"])
            payload = json.dumps(it, ensure_ascii=False)
            cur = c.execute(
                "SELECT payload FROM seen WHERE tenant_id=? AND site=? AND kind=? AND item_id=?",
                (tenant_id, site, kind, iid),
            )
            row = cur.fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO seen (tenant_id, site, kind, item_id, payload) VALUES (?,?,?,?,?)",
                    (tenant_id, site, kind, iid, payload),
                )
                new.append(it)
            elif row[0] != payload:
                # Seen before but content changed (e.g. detail backfilled).
                c.execute(
                    "UPDATE seen SET payload=? WHERE tenant_id=? AND site=? AND kind=? AND item_id=?",
                    (payload, tenant_id, site, kind, iid),
                )
    return new


def get_recent(tenant_id: str, site: str, kind: str, limit: int = 20) -> list[dict]:
    """Return the most recently seen items of a kind, newest first.

    Each returned dict is the stored payload augmented with `first_seen`.
    """
    out: list[dict] = []
    with _conn() as c:
        rows = c.execute(
            """SELECT payload, first_seen FROM seen
               WHERE tenant_id=? AND site=? AND kind=?
               ORDER BY first_seen DESC, rowid DESC
               LIMIT ?""",
            (tenant_id, site, kind, limit),
        ).fetchall()
    for payload, first_seen in rows:
        item = _parse_payload(payload)
        item["first_seen"] = first_seen
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Responder decisions
# ---------------------------------------------------------------------------

_RESPONSE_FIELDS = ("status", "unit_id", "reason", "draft", "confidence", "tenant_email")


def save_response(tenant_id: str, site: str, kind: str, item_id: str, **fields) -> None:
    """Insert or replace the responder's decision for one lead."""
    cols = ["tenant_id", "site", "kind", "item_id"] + [f for f in _RESPONSE_FIELDS if f in fields]
    vals = [tenant_id, site, kind, item_id] + [fields[f] for f in _RESPONSE_FIELDS if f in fields]
    placeholders = ",".join("?" * len(cols))
    with _conn() as c:
        c.execute(
            f"INSERT OR REPLACE INTO responses ({','.join(cols)}) VALUES ({placeholders})",
            vals,
        )


def update_response(tenant_id: str, site: str, item_id: str, **fields) -> None:
    """Patch fields on an existing response row (e.g. mark sent)."""
    allowed = _RESPONSE_FIELDS + ("sent_at", "emailed_at")
    sets = [f for f in fields if f in allowed]
    if not sets:
        return
    assignments = ", ".join(f"{f}=?" for f in sets)
    vals = [fields[f] for f in sets] + [tenant_id, site, item_id]
    with _conn() as c:
        c.execute(
            f"UPDATE responses SET {assignments} WHERE tenant_id=? AND site=? AND item_id=?",
            vals,
        )


def get_responses(tenant_id: str, site: str) -> dict[str, dict]:
    """Return all responder decisions for a tenant+site, keyed by item_id."""
    out: dict[str, dict] = {}
    with _conn() as c:
        rows = c.execute(
            """SELECT item_id, status, unit_id, reason, draft, confidence,
                      tenant_email, created_at, sent_at, emailed_at
               FROM responses WHERE tenant_id=? AND site=?""",
            (tenant_id, site),
        ).fetchall()
    keys = ("item_id", "status", "unit_id", "reason", "draft", "confidence",
            "tenant_email", "created_at", "sent_at", "emailed_at")
    for row in rows:
        rec = dict(zip(keys, row))
        out[rec["item_id"]] = rec
    return out
