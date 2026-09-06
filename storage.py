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


def _ddl(c) -> None:
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
            -- The second dedup key and the forward bit; see `_already_ingested`.
            -- NULL alt_id means the row predates them, not "no second key".
            alt_id TEXT,
            via_forward INTEGER NOT NULL DEFAULT 0,
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
    # A destructive rebuild (CREATE/INSERT SELECT/DROP/RENAME), not idempotent
    # DDL. It stays inside the schema step so it runs under the advisory lock
    # and commits before any caller statement — never concurrently with one.
    _migrate_tenant_id(c)

    # Runs *after* the rebuild, unlike the `responses` migration above: that
    # rebuild copies a fixed column list, so a column added before it would be
    # silently dropped on the way through. Rows written before these columns
    # existed keep alt_id NULL, and `_already_ingested` reads that NULL as
    # "this row's strict key was never recorded" rather than inventing one.
    seen_have = db.table_columns(c, "seen")
    for col, decl in (("alt_id", "TEXT"),
                      ("via_forward", "INTEGER NOT NULL DEFAULT 0")):
        if col not in seen_have:
            c.execute(f"ALTER TABLE seen ADD COLUMN {col} {decl}")
    c.execute(
        "CREATE INDEX IF NOT EXISTS seen_alt_id "
        "ON seen (tenant_id, site, kind, alt_id)"
    )


def _conn():
    return db.open_with_schema("storage", _ddl)


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


# Keys an item carries for the dedup decision but that are not part of the item
# itself. Stripped before the payload is stored so a row written by this version
# is byte-identical to one written before these existed — otherwise every
# pre-existing row would look "changed" on its next pass and be rewritten.
_DEDUP_HINTS = ("dedup_id", "via_forward")


def _payload_of(it: dict) -> str:
    return json.dumps({k: v for k, v in it.items() if k not in _DEDUP_HINTS},
                      ensure_ascii=False)


def _already_ingested(c, tenant_id: str, site: str, kind: str, it: dict) -> bool:
    """Whether this item is a copy of one already ingested, on either key.

    An item may carry a second, looser key (`dedup_id`) alongside its `id`; see
    the id derivation in `sites/ff_email.py` for why one key cannot do this job.
    The strict key separates two messages sent on the same day but is rewritten
    by every relay; the loose key survives relays but cannot tell two same-day
    sends apart. So a loose match alone is not enough to drop something — the
    question is whether the loose match is a *copy* or a *second message*, and
    two kinds of answer settle it.

    A forward says the loose match is a copy:

      - the incoming mail is itself a forward (`via_forward`), so it is a copy
        of the notification rather than a new one;
      - the stored row arrived as a forward, so this direct delivery is the
        original it was made from — the same pair in the other arrival order,
        which happens when a host forwards their backlog before the live feed
        is pointed at us.

    Or one of the two sides has no key finer than the loose one, in which case
    a copy and a second message are genuinely indistinguishable and the only
    safe reading is the one this table had before the strict key existed:

      - the stored row predates the strict key (alt_id NULL). Its strict key was
        never recorded, so a strict miss against it proves nothing. These rows
        are why the loose key is the unchanged one — an open conversation stays
        addressable under the key it was written with.
      - the stored row was written from a delivery that carried no transport
        stamp (alt_id equal to item_id), or *this* delivery carries none
        (`loose == iid`). Both happen: `dashboard.py`'s retry re-parses a held
        email with `row.get("mail_date") or ""`, so a row stored without a date
        re-derives the message with no transport stamp at all. Without these
        two clauses that redelivery reports unseen and recovery writes the
        board again — a duplicate deal and a second reply to a guest who wrote
        once, which is a guarantee the single-key table did have.

    In all four of those the original defect stays live for the pair involved,
    which is the pre-existing behaviour rather than a new loss. Two of them are
    the ticket's own symptom on a path this change does not reach: when either
    copy carries a forward banner, two same-day messages with the same words
    still collapse, because the input is byte-identical to a re-forward of one
    message and separating them needs a per-message discriminator the parser
    does not have. `tests/test_ven155_same_day_messages.py` holds that as a
    strict xfail so it stays executable rather than implicit.

    Everything else — a loose match with no forward on either side, where both
    sides recorded a strict key and those keys differ — is a guest writing
    twice, and is let through.
    """
    iid = str(it["id"])
    if c.execute(
        "SELECT 1 FROM seen WHERE tenant_id=? AND site=? AND kind=? AND item_id=?",
        (tenant_id, site, kind, iid),
    ).fetchone():
        return True

    loose = str(it.get("dedup_id") or "")
    if not loose:
        # No second key at all — the item never joined the two-key scheme (a
        # lead, say), so the check above was the whole question. `loose == iid`
        # is *not* this case: it means this delivery had nothing more precise to
        # offer, and a stored row may still be more precise than it.
        return False

    # `item_id=?` as well as `alt_id=?`: a row written before the strict key
    # existed holds the loose key in item_id, which is the column it was the
    # primary key of at the time.
    rows = c.execute(
        "SELECT item_id, alt_id, via_forward FROM seen "
        "WHERE tenant_id=? AND site=? AND kind=? AND (alt_id=? OR item_id=?)",
        (tenant_id, site, kind, loose, loose),
    ).fetchall()
    for row_id, alt_id, stored_via_forward in rows:
        if not alt_id or alt_id == row_id or loose == iid:
            # One of the two sides has no key finer than the loose one: the row
            # predates the strict key, or it was written from a delivery that
            # carried no transport stamp, or this delivery carries none. With
            # nothing better than a day to compare, a copy and a second message
            # are indistinguishable, so keep the reading this table had before
            # the strict key existed.
            return True
        if it.get("via_forward") or stored_via_forward:
            return True
    return False


def filter_new(tenant_id: str, site: str, kind: str, items: Iterable[dict]) -> list[dict]:
    """Record items and return only the ones not seen before (for this tenant).

    Brand-new items are inserted and returned (so they get notified/drafted).
    Already-seen items are NOT returned, but their stored payload is refreshed
    when the freshly-scraped payload differs — this lets a re-scrape backfill
    richer data (e.g. the lead detail view) onto existing rows without
    re-notifying. `first_seen` is preserved so ordering stays stable.

    "Seen before" is `_already_ingested`, which may consult a second key; items
    without one behave exactly as they did when `item_id` was the only key.
    """
    new = []
    with _conn() as c:
        for it in items:
            iid = str(it["id"])
            payload = _payload_of(it)
            if _already_ingested(c, tenant_id, site, kind, it):
                cur = c.execute(
                    "SELECT payload FROM seen WHERE tenant_id=? AND site=? AND kind=? AND item_id=?",
                    (tenant_id, site, kind, iid),
                )
                row = cur.fetchone()
                # A row matched on the loose key is a *copy* of this item, not
                # this item, so there is nothing under `iid` to refresh and the
                # copy must not overwrite the original's payload.
                if row is not None and row[0] != payload:
                    # Seen before but content changed (e.g. detail backfilled).
                    c.execute(
                        "UPDATE seen SET payload=? WHERE tenant_id=? AND site=? AND kind=? AND item_id=?",
                        (payload, tenant_id, site, kind, iid),
                    )
                continue
            c.execute(
                "INSERT INTO seen (tenant_id, site, kind, item_id, payload, alt_id, via_forward) "
                "VALUES (?,?,?,?,?,?,?)",
                (tenant_id, site, kind, iid, payload,
                 str(it.get("dedup_id") or "") or None,
                 1 if it.get("via_forward") else 0),
            )
            new.append(it)
    return new


def already_seen(tenant_id: str, site: str, kind: str, item_id: str,
                 item: dict | None = None) -> bool:
    """Whether this item has been ingested before — without recording it.

    `filter_new` answers the same question but *records as it asks*, which makes
    it useless to a caller that needs to know beforehand whether to act: asking
    is indistinguishable from consuming. Recovery needs to tell "this message was
    already applied" from "this message is new", and must not mark the second one
    seen until the board write has actually happened.

    Pass `item` when the caller holds the parsed item: the answer then uses the
    same two-key rule `filter_new` applies, so a caller that asks first and
    records afterwards does not get two different answers about one message.
    With only an id it degrades to the strict key, which is the whole question
    for anything that never carried a second one.
    """
    if not item_id:
        return False
    probe = dict(item) if item else {}
    probe["id"] = str(item_id)
    with _conn() as c:
        return _already_ingested(c, tenant_id, site, kind, probe)


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
