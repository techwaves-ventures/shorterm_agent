"""Dedup + responder store. Returns only items not seen before.

Storage runs on either SQLite (local/dev, file at SQLITE_PATH) or a hosted
Postgres instance (DATABASE_URL) — see db.py. This module speaks the shared
SQLite-flavoured dialect and lets db.connect() translate for Postgres, so a
Vercel/Neon deploy persists leads without a mounted disk. DB_PATH is re-exported
from db so existing `from storage import DB_PATH` imports keep working.
"""
import ast
import json
from typing import Iterable

import db
from db import DB_PATH  # re-exported for backward-compatible imports


def _conn():
    c = db.connect()
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
    have = db.table_columns(c, "responses")
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
    seen_cols = db.table_columns(c, "seen")
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

    resp_cols = db.table_columns(c, "responses")
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


def already_seen(tenant_id: str, site: str, kind: str, item_id: str) -> bool:
    """Whether this item has been ingested before — without recording it.

    `filter_new` answers the same question but *records as it asks*, which makes
    it useless to a caller that needs to know beforehand whether to act: asking
    is indistinguishable from consuming. Recovery needs to tell "this message was
    already applied" from "this message is new", and must not mark the second one
    seen until the board write has actually happened.
    """
    if not item_id:
        return False
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM seen WHERE tenant_id=? AND site=? AND kind=? AND item_id=?",
            (tenant_id, site, kind, str(item_id)),
        ).fetchone()
    return row is not None


def forget(tenant_id: str, site: str, kind: str, item_id: str) -> bool:
    """Drop an item's dedup row so it can be ingested again. True if one went.

    The counterpart to `filter_new` recording *before* the work happens. That
    ordering is deliberate — it is a single atomic claim, so two concurrent
    deliveries of one item can't both proceed — but it means a caller whose
    downstream write then fails has already promised the item was handled.
    Leaving that row is the silent loss the inbound-rejects table exists to end:
    nothing is on the board, and every later delivery short-circuits at the
    dedup and never reaches the board either.

    So a caller that records first must be able to take it back. `seen` then
    carries an invariant worth relying on — an item marked seen actually landed
    — which is what lets the retry path stop guessing from presence.
    """
    if not item_id:
        return False
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM seen WHERE tenant_id=? AND site=? AND kind=? AND item_id=?",
            (tenant_id, site, kind, str(item_id)),
        )
        return bool(cur.rowcount)


def get_recent(tenant_id: str, site: str, kind: str, limit: int = 20) -> list[dict]:
    """Return the most recently seen items of a kind, newest first.

    Each returned dict is the stored payload augmented with `first_seen`.
    Ordering is newest `first_seen` first, tie-broken by `item_id` for a
    deterministic result on both engines (SQLite's implicit `rowid` doesn't
    exist in Postgres; ties only occur within the same one-second timestamp).
    """
    out: list[dict] = []
    with _conn() as c:
        rows = c.execute(
            """SELECT payload, first_seen FROM seen
               WHERE tenant_id=? AND site=? AND kind=?
               ORDER BY first_seen DESC, item_id DESC
               LIMIT ?""",
            (tenant_id, site, kind, limit),
        ).fetchall()
    for payload, first_seen in rows:
        item = _parse_payload(payload)
        item["first_seen"] = first_seen
        out.append(item)
    return out


def all_items(tenant_id: str, site: str) -> dict[str, dict]:
    """Every stored item for a tenant+site, keyed by item_id.

    One query for the whole board: the dashboard joins deals to their scraped
    payloads, and doing that per-deal would be N round-trips per page load.
    """
    out: dict[str, dict] = {}
    with _conn() as c:
        rows = c.execute(
            "SELECT item_id, payload, first_seen, kind FROM seen "
            "WHERE tenant_id=? AND site=?",
            (tenant_id, site),
        ).fetchall()
    for item_id, payload, first_seen, kind in rows:
        item = _parse_payload(payload)
        item["first_seen"] = first_seen
        item.setdefault("kind", kind)
        item.setdefault("id", item_id)
        out[item_id] = item
    return out


def items_by_ids(tenant_id: str, site: str, item_ids) -> dict[str, dict]:
    """Stored items for a specific set of ids, keyed by item_id.

    The inbox renders one page at a time, so it wants 25 payloads — not the
    tenant's entire mailbox (`all_items`) and not 25 round-trips (`get_item` in
    a loop). Ids come from our own query, never from the request.
    """
    ids = [str(i) for i in item_ids]
    if not ids:
        return {}
    out: dict[str, dict] = {}
    placeholders = ",".join("?" * len(ids))
    with _conn() as c:
        rows = c.execute(
            f"""SELECT item_id, payload, first_seen, kind FROM seen
                WHERE tenant_id=? AND site=? AND item_id IN ({placeholders})""",
            [tenant_id, site] + ids,
        ).fetchall()
    for item_id, payload, first_seen, kind in rows:
        item = _parse_payload(payload)
        item["first_seen"] = first_seen
        item.setdefault("kind", kind)
        item.setdefault("id", item_id)
        out[item_id] = item
    return out


def get_item(tenant_id: str, site: str, item_id: str) -> dict | None:
    """One stored item by id, regardless of kind (tagged with its kind).

    Direct lookup on the primary key — the automation scheduler resolves deals
    back to their scraped payload constantly, and scanning get_recent() for that
    would be O(n) per deal.
    """
    with _conn() as c:
        row = c.execute(
            """SELECT payload, first_seen, kind FROM seen
               WHERE tenant_id=? AND site=? AND item_id=?""",
            (tenant_id, site, str(item_id)),
        ).fetchone()
    if not row:
        return None
    item = _parse_payload(row[0])
    item["first_seen"] = row[1]
    item.setdefault("kind", row[2])
    return item


# ---------------------------------------------------------------------------
# Responder decisions
# ---------------------------------------------------------------------------

_RESPONSE_FIELDS = ("status", "unit_id", "reason", "draft", "confidence", "tenant_email")


def save_response(tenant_id: str, site: str, kind: str, item_id: str, **fields) -> None:
    """Upsert the responder's decision for one lead (keyed by tenant+site+item).

    Uses ON CONFLICT (portable across SQLite and Postgres) instead of SQLite's
    INSERT OR REPLACE. On conflict it updates the columns supplied this call and
    leaves the rest (e.g. sent_at) intact.
    """
    provided = [f for f in _RESPONSE_FIELDS if f in fields]
    cols = ["tenant_id", "site", "kind", "item_id"] + provided
    vals = [tenant_id, site, kind, item_id] + [fields[f] for f in provided]
    placeholders = ",".join("?" * len(cols))
    updates = ", ".join(f"{c2}=excluded.{c2}" for c2 in ["kind"] + provided)
    with _conn() as c:
        c.execute(
            f"INSERT INTO responses ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT (tenant_id, site, item_id) DO UPDATE SET {updates}",
            vals,
        )


def update_response(tenant_id: str, site: str, item_id: str, **fields) -> None:
    """Patch fields on a response row, creating it if there isn't one yet.

    An upsert rather than a bare UPDATE, because "no row yet" is not a rare
    case: a deal opened by `pipeline.backfill` goes through `pipeline.ensure`
    only, which writes no `responses` row at all. Marking such a reply `sent`
    then updated nothing and reported success, so `status` stayed unset — and
    both things that gate the second send read that status. The thread page went
    on showing an enabled "Approve & send" over a message the guest had already
    received, and the 409 behind it did not fire either. The guard failed
    *open*, which is the dangerous direction for a duplicate-message bug.
    """
    allowed = _RESPONSE_FIELDS + ("sent_at", "emailed_at")
    sets = [f for f in fields if f in allowed]
    if not sets:
        return
    assignments = ", ".join(f"{f}=?" for f in sets)
    vals = [fields[f] for f in sets] + [tenant_id, site, str(item_id)]
    with _conn() as c:
        cur = c.execute(
            f"UPDATE responses SET {assignments} WHERE tenant_id=? AND site=? AND item_id=?",
            vals,
        )
        if getattr(cur, "rowcount", 0) > 0:
            return
        # `kind` and `status` are NOT NULL. Take the kind from the stored item
        # so the new row matches what `save_response` would have written, and
        # only default the status when the caller isn't setting one.
        row = c.execute(
            "SELECT kind FROM seen WHERE tenant_id=? AND site=? AND item_id=? LIMIT 1",
            (tenant_id, site, str(item_id)),
        ).fetchone()
        cols = ["tenant_id", "site", "kind", "item_id"] + sets
        values = [tenant_id, site, (row[0] if row else None) or "lead",
                  str(item_id)] + [fields[f] for f in sets]
        if "status" not in sets:
            cols.append("status")
            values.append("draft")
        c.execute(
            f"INSERT INTO responses ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            values,
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
