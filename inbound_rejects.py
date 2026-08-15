"""Forwarded emails we could not turn into a lead — kept where an operator sees them.

`inbound.accept` drops anything it can't confidently read. That is the right
call for the *pipeline* (a half-read lead is worse than none), but the endpoint
answers 202 and the mail provider never retries, so the enquiry simply ceased to
exist. Nothing appeared in the UI and the host had no way to know a guest wrote
to them. This table is that missing record: every future parser gap degrades
from "silent lead loss" to "a row the operator can retry".

## What is stored, and what deliberately is not

`/inbound/email` is a *public, unauthenticated* ingress with a 512 KB cap.
Persisting every rejection would make it an unauthenticated write amplifier —
500 junk POSTs is 256 MB of attacker-chosen content rendered into the operator's
browser. So only rejections raised *after* the provider secret verified *and* a
tenant was resolved are recorded (`inbound.RECORDABLE_CODES`); everything
earlier is logged and forgotten. See `inbound.Rejected`.

The stored body is the output of `inbound.extract_body`, i.e. exactly the text
the parser was given — not the raw payload. Retry therefore re-runs the current
parser over the same input the original attempt failed on, and we hold less of
the message than the provider sent.

Bounds, because these rows hold guest PII and arrive from outside: body
truncated to `MAX_STORED_BODY`, at most `MAX_ROWS_PER_TENANT` rows per
tenant+site (newest kept), and identical replays collapse onto one row with a
bumped `seen_count` rather than a new row.
"""
import hashlib
import logging
from datetime import datetime, timezone

import db

log = logging.getLogger(__name__)

OPEN = "open"
DISMISSED = "dismissed"
RECOVERED = "recovered"

MAX_STORED_BODY = 8_000
MAX_ROWS_PER_TENANT = 200
_MAX_SUBJECT = 500
_MAX_SENDER = 320          # RFC 5321 maximum path length
_MAX_REASON = 500

_COLS = (
    "id", "tenant_id", "site", "reason_code", "reason", "subject", "sender",
    "body", "fingerprint", "seen_count", "received_at", "status", "resolved_at",
    "resolved_item_id",
)

_SELECT = f"SELECT {', '.join(_COLS)} FROM inbound_rejects"


def _now() -> str:
    """Absolute, offset-carrying UTC.

    Deliberately never `CURRENT_TIMESTAMP`: that writes the *database server's*
    local wall clock with no offset, so a second host reading it back cannot
    tell what instant it means. On VEN-127 exactly that mistake — a naive local
    stamp compared across hosts — double-delivered a live message to a guest.
    One writer, one format, offset always present.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conn() -> db.Conn:
    c = db.connect()
    c.execute(
        """CREATE TABLE IF NOT EXISTS inbound_rejects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            site TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            reason TEXT,
            subject TEXT,
            sender TEXT,
            body TEXT,
            fingerprint TEXT NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 1,
            received_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            resolved_at TEXT,
            resolved_item_id TEXT
        )"""
    )
    # Carries the dedup: an identical replay lands on the same row. Also the
    # conflict target of the upsert in `record`, so it must exist first.
    c.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS ix_inbound_rejects_fp
           ON inbound_rejects (tenant_id, site, fingerprint)"""
    )
    return c


def _row(r) -> dict:
    return dict(zip(_COLS, r))


def _fingerprint(subject: str, sender: str, body: str) -> str:
    raw = "\x00".join((subject, sender, body)).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()


def _prune(c: db.Conn, tenant_id: str, site: str) -> None:
    """Keep only the newest `MAX_ROWS_PER_TENANT` rows for this tenant+site.

    Postgres has no `DELETE ... LIMIT`, so the bound goes in a subselect. This
    is the portable form; the SQLite-only shorthand passes tests locally and
    fails on the hosted database.
    """
    c.execute(
        """DELETE FROM inbound_rejects
           WHERE tenant_id=? AND site=? AND id NOT IN (
               SELECT id FROM inbound_rejects
               WHERE tenant_id=? AND site=?
               ORDER BY id DESC LIMIT ?
           )""",
        (tenant_id, site, tenant_id, site, MAX_ROWS_PER_TENANT),
    )


def record(tenant_id: str, site: str, reason_code: str, reason: str,
           payload: dict) -> int | None:
    """Store one rejected inbound message. Returns the row id.

    Callers must have already established that this rejection is worth keeping
    (see the module docstring); this function does not re-check authentication.
    """
    import inbound  # local: inbound imports nothing from here, keep it that way

    subject = inbound.extract_subject(payload)[:_MAX_SUBJECT]
    sender = inbound.extract_sender(payload)[:_MAX_SENDER]
    body = inbound.extract_body(payload)[:MAX_STORED_BODY]
    fp = _fingerprint(subject, sender, body)
    now = _now()

    with _conn() as c:
        # An exact replay bumps the counter instead of adding a row. `status` is
        # deliberately NOT reset: a row the operator already dismissed or
        # recovered stays that way, otherwise Dismiss would be undone by the
        # next duplicate forward and the list could never be cleared.
        c.execute(
            """INSERT INTO inbound_rejects
                   (tenant_id, site, reason_code, reason, subject, sender, body,
                    fingerprint, seen_count, received_at, status)
               VALUES (?,?,?,?,?,?,?,?,1,?,?)
               ON CONFLICT (tenant_id, site, fingerprint) DO UPDATE SET
                   seen_count = inbound_rejects.seen_count + 1,
                   received_at = excluded.received_at,
                   reason_code = excluded.reason_code,
                   reason = excluded.reason""",
            (tenant_id, site, reason_code, (reason or "")[:_MAX_REASON], subject,
             sender, body, fp, now, OPEN),
        )
        # Read the id back rather than using lastrowid/RETURNING: on SQLite
        # lastrowid is not updated when an upsert takes the DO UPDATE path, so
        # a duplicate would report the id of some unrelated earlier insert.
        row = c.execute(
            "SELECT id FROM inbound_rejects WHERE tenant_id=? AND site=? AND fingerprint=?",
            (tenant_id, site, fp),
        ).fetchone()
        _prune(c, tenant_id, site)
    return row[0] if row else None


def open_for_tenant(tenant_id: str, site: str) -> list[dict]:
    """Unresolved rejections, newest first — what the review page lists."""
    with _conn() as c:
        rows = c.execute(
            f"{_SELECT} WHERE tenant_id=? AND site=? AND status=? ORDER BY id DESC",
            (tenant_id, site, OPEN),
        ).fetchall()
    return [_row(r) for r in rows]


def count_open(tenant_id: str, site: str) -> int:
    """How many leads are currently sitting unread. Drives the dashboard banner."""
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM inbound_rejects WHERE tenant_id=? AND site=? AND status=?",
            (tenant_id, site, OPEN),
        ).fetchone()
    return int(row[0]) if row else 0


def count_all(tenant_id: str, site: str) -> int:
    """Every retained row, resolved or not — the denominator on the settings line."""
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM inbound_rejects WHERE tenant_id=? AND site=?",
            (tenant_id, site),
        ).fetchone()
    return int(row[0]) if row else 0


def get(tenant_id: str, site: str, rid: int) -> dict | None:
    """One row, scoped to its owner.

    Tenant id is part of the lookup rather than checked afterwards, so there is
    no path that reads another tenant's row into memory at all.
    """
    with _conn() as c:
        row = c.execute(
            f"{_SELECT} WHERE id=? AND tenant_id=? AND site=?",
            (rid, str(tenant_id), site),
        ).fetchone()
    return _row(row) if row else None


def dismiss(tenant_id: str, site: str, rid: int) -> bool:
    """Take a row off the open list, keeping it as an audit record."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE inbound_rejects SET status=?, resolved_at=? "
            "WHERE id=? AND tenant_id=? AND site=? AND status=?",
            (DISMISSED, _now(), rid, str(tenant_id), site, OPEN),
        )
        return bool(cur.rowcount)


def mark_recovered(tenant_id: str, site: str, rid: int, item_id: str) -> bool:
    """A retry parsed: link the row to the deal it became.

    Guarded on `status=OPEN` so two retries of the same row cannot both proceed
    to create work — the second sees no rows updated and stops.
    """
    with _conn() as c:
        cur = c.execute(
            "UPDATE inbound_rejects SET status=?, resolved_at=?, resolved_item_id=? "
            "WHERE id=? AND tenant_id=? AND site=? AND status=?",
            (RECOVERED, _now(), str(item_id), rid, str(tenant_id), site, OPEN),
        )
        return bool(cur.rowcount)


def reopen(tenant_id: str, site: str, rid: int, reason: str) -> None:
    """Put a row back on the list after a recovery attempt failed part-way.

    The claim in `mark_recovered` happens before the lead is stored, so that two
    retries can't both create a deal. If the store then fails, this undoes the
    claim — otherwise the row would read as recovered with nothing on the board,
    which is the silent loss this table exists to end.
    """
    with _conn() as c:
        c.execute(
            "UPDATE inbound_rejects SET status=?, resolved_at=NULL, "
            "resolved_item_id=NULL, reason=? WHERE id=? AND tenant_id=? AND site=?",
            (OPEN, (reason or "")[:_MAX_REASON], rid, str(tenant_id), site),
        )


def update_reason(tenant_id: str, site: str, rid: int, reason: str) -> None:
    """Refresh the failure detail after a retry that still could not parse."""
    with _conn() as c:
        c.execute(
            "UPDATE inbound_rejects SET reason=? WHERE id=? AND tenant_id=? AND site=?",
            ((reason or "")[:_MAX_REASON], rid, str(tenant_id), site),
        )
