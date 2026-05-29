"""SQLite-backed dedup store. Returns only items not seen before."""
import ast
import json
import sqlite3
from pathlib import Path
from typing import Iterable

DB_PATH = Path(__file__).parent / "leads.db"


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.execute(
        """CREATE TABLE IF NOT EXISTS seen (
            site TEXT NOT NULL,
            kind TEXT NOT NULL,
            item_id TEXT NOT NULL,
            payload TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (site, kind, item_id)
        )"""
    )
    # The responder agent's decision for each lead. One row per (site, item_id).
    # status: draft | skipped | sent | dismissed
    c.execute(
        """CREATE TABLE IF NOT EXISTS responses (
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
            PRIMARY KEY (site, item_id)
        )"""
    )
    # Idempotent migration: add columns that older leads.db files lack.
    have = {row[1] for row in c.execute("PRAGMA table_info(responses)")}
    for col, decl in (("tenant_email", "TEXT"), ("emailed_at", "TIMESTAMP")):
        if col not in have:
            c.execute(f"ALTER TABLE responses ADD COLUMN {col} {decl}")
    return c


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


def filter_new(site: str, kind: str, items: Iterable[dict]) -> list[dict]:
    """Return items whose item_id hasn't been recorded yet, and record them."""
    new = []
    with _conn() as c:
        for it in items:
            iid = str(it["id"])
            cur = c.execute(
                "SELECT 1 FROM seen WHERE site=? AND kind=? AND item_id=?",
                (site, kind, iid),
            )
            if cur.fetchone() is None:
                c.execute(
                    "INSERT INTO seen (site, kind, item_id, payload) VALUES (?,?,?,?)",
                    (site, kind, iid, json.dumps(it, ensure_ascii=False)),
                )
                new.append(it)
    return new


def get_recent(site: str, kind: str, limit: int = 20) -> list[dict]:
    """Return the most recently seen items of a kind, newest first.

    Each returned dict is the stored payload augmented with `first_seen`.
    """
    out: list[dict] = []
    with _conn() as c:
        rows = c.execute(
            """SELECT payload, first_seen FROM seen
               WHERE site=? AND kind=?
               ORDER BY first_seen DESC, rowid DESC
               LIMIT ?""",
            (site, kind, limit),
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


def save_response(site: str, kind: str, item_id: str, **fields) -> None:
    """Insert or replace the responder's decision for one lead."""
    cols = ["site", "kind", "item_id"] + [f for f in _RESPONSE_FIELDS if f in fields]
    vals = [site, kind, item_id] + [fields[f] for f in _RESPONSE_FIELDS if f in fields]
    placeholders = ",".join("?" * len(cols))
    with _conn() as c:
        c.execute(
            f"INSERT OR REPLACE INTO responses ({','.join(cols)}) VALUES ({placeholders})",
            vals,
        )


def update_response(site: str, item_id: str, **fields) -> None:
    """Patch fields on an existing response row (e.g. mark sent)."""
    allowed = _RESPONSE_FIELDS + ("sent_at", "emailed_at")
    sets = [f for f in fields if f in allowed]
    if not sets:
        return
    assignments = ", ".join(f"{f}=?" for f in sets)
    vals = [fields[f] for f in sets] + [site, item_id]
    with _conn() as c:
        c.execute(
            f"UPDATE responses SET {assignments} WHERE site=? AND item_id=?",
            vals,
        )


def get_responses(site: str) -> dict[str, dict]:
    """Return all responder decisions for a site, keyed by item_id."""
    out: dict[str, dict] = {}
    with _conn() as c:
        rows = c.execute(
            """SELECT item_id, status, unit_id, reason, draft, confidence,
                      tenant_email, created_at, sent_at, emailed_at
               FROM responses WHERE site=?""",
            (site,),
        ).fetchall()
    keys = ("item_id", "status", "unit_id", "reason", "draft", "confidence",
            "tenant_email", "created_at", "sent_at", "emailed_at")
    for row in rows:
        rec = dict(zip(keys, row))
        out[rec["item_id"]] = rec
    return out
