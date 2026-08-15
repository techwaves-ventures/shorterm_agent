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
tenant+site, and identical replays collapse onto one row with a bumped
`seen_count` rather than a new row.

At the cap the survivors are chosen by usefulness, not recency — see `_KEEP_IDS`
for what that ordering can and cannot separate. Known bound: FurnishedFinder's
own digests and reminders come *from the allowed sender* and so land in the same
`unparsed` bucket as a lead we failed to read. A tenant who reaches 200 open
`unparsed` rows can therefore have a genuine enquiry evicted by that mail. The
eviction is logged (a pruned unreviewed row always warns), so it is bounded and
visible rather than silent, but separating the two would need bulk-mail headers
this table does not keep.
"""
import functools
import hashlib
import logging
import threading
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

_MAX_MAIL_DATE = 120       # a Date header; anything longer is not one

# What the review page shows for a freshly captured row. `reason` is the only
# per-row explanation the operator ever sees, and after a failed retry it is
# overwritten with what that attempt actually hit — so it has to read as a
# sentence a host can act on the whole time. `str(Rejected)` is an internal
# audit string (and for `sender_not_allowed` it embeds the raw sender), which
# belongs in the log line at the call site, not on their screen.
_CAPTURE_REASON = {
    "unparsed": "Couldn't find a guest name and a property or date in this email.",
    "sender_not_allowed": "Sent from an address we don't recognise as FurnishedFinder.",
}

# What a failed retry leaves on the row. Every retry ends at the parser, but the
# parser is not every row's problem: on a `sender_not_allowed` row the host's one
# actionable fact is the allowlist, and replacing it with parse-flavoured text
# takes the only thing they could have acted on off their screen while telling
# them something they can't fix. So the copy stays keyed on why the row was
# captured, and says what the attempt did without discarding the diagnosis.
_RETRY_FAILED_REASON = {
    "unparsed": "Tried again — still couldn't read this email.",
    "sender_not_allowed": (
        "Sent from an address we don't recognise as FurnishedFinder. Tried "
        "reading it anyway, without success — add the sender to keep these."
    ),
}


def retry_failed_reason(reason_code: str) -> str:
    """Operator-facing copy for a retry that still could not parse the email."""
    return _RETRY_FAILED_REASON.get(reason_code or "") or _RETRY_FAILED_REASON["unparsed"]


# What a retry leaves on the row when the email *read* fine and the board write
# is what failed. Same rule as `_RETRY_FAILED_REASON`, one branch over: the
# outcome of the attempt is news worth showing, but it is not a reason to delete
# the diagnosis the row was captured with. A `sender_not_allowed` row still has
# exactly one thing its host can do about it, and "try again" is not that thing.
_BOARD_FAILED_REASON = {
    "unparsed": "Read this email, but couldn't open the lead — try again.",
    "sender_not_allowed": (
        "Sent from an address we don't recognise as FurnishedFinder. Read it, but "
        "couldn't open the lead — try again, and add the sender to keep these."
    ),
}


def board_failed_reason(reason_code: str) -> str:
    """Operator-facing copy for a retry that parsed but never reached the board."""
    return _BOARD_FAILED_REASON.get(reason_code or "") or _BOARD_FAILED_REASON["unparsed"]


_COLS = (
    "id", "tenant_id", "site", "reason_code", "reason", "subject", "sender",
    "body", "fingerprint", "seen_count", "received_at", "mail_date", "status",
    "resolved_at", "resolved_item_id",
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


# Which database the schema below was verified against, so a process that is
# repointed at another one (tests do this) re-checks rather than trusting a
# memo about a different file.
_schema_ready: str | None = None
_schema_lock = threading.Lock()


def _db_identity() -> str:
    return f"{db.backend()}\x00{db.database_url()}\x00{db.DB_PATH}"


# These predicates now *gate* the DDL rather than merely informing it, so each
# has to identify the object this module will actually write to. `IF NOT EXISTS`
# used to be the backstop for a loose answer; there is no backstop now, and a
# false "it is already there" leaves `record` failing on every later call behind
# a 202. On Postgres they therefore resolve through `to_regclass`, which honours
# search_path and answers about one specific relation, instead of matching a
# bare name anywhere in the database.
def _table_exists(c: db.Conn) -> bool:
    if c.pg:
        row = c.execute("SELECT to_regclass('inbound_rejects')").fetchone()
        return bool(row and row[0])
    return bool(db.table_columns(c, "inbound_rejects"))


def _has_mail_date(c: db.Conn) -> bool:
    if c.pg:
        row = c.execute(
            "SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('inbound_rejects') "
            "AND attname = ? AND NOT attisdropped", ("mail_date",),
        ).fetchone()
        return bool(row)
    return "mail_date" in db.table_columns(c, "inbound_rejects")


def _index_exists(c: db.Conn, name: str, unique: bool = False) -> bool:
    if c.pg:
        # The name alone is not the question. The same name on another table or
        # in another schema would skip the index `record`'s ON CONFLICT needs;
        # and a *non-unique* index of the right name satisfies `CREATE ... IF NOT
        # EXISTS` while failing that upsert, so uniqueness is part of it too.
        row = c.execute(
            "SELECT 1 FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
            "WHERE i.relname = ? AND x.indrelid = to_regclass('inbound_rejects') "
            "AND (x.indisunique OR NOT ?)", (name, bool(unique)),
        ).fetchone()
    else:
        row = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=? "
            "AND tbl_name='inbound_rejects'", (name,),
        ).fetchone()
    return bool(row)


def _add_mail_date(c: db.Conn) -> None:
    # Added after the table shipped, so existing deployments need the migration.
    # Nullable on purpose: rows captured before this can never learn their mail
    # `Date`, and retry falls back to `received_at` for them (see the retry
    # route). A NOT NULL default would invent a stamp that looks authoritative.
    if c.pg:
        c.execute("ALTER TABLE inbound_rejects ADD COLUMN IF NOT EXISTS mail_date TEXT")
    else:
        # SQLite has no IF NOT EXISTS for ADD COLUMN; the caller re-checks.
        c.execute("ALTER TABLE inbound_rejects ADD COLUMN mail_date TEXT")


def _ensure_object(exists, apply) -> None:
    """Create one schema object on a connection of its own.

    Each attempt is its own short transaction that carries *no* DML, which is
    the entire point — see `_ensure_schema`. Losing a create race is success,
    because the only thing it proves is that the object is there; but that is
    checked rather than assumed, so a genuinely broken migration still raises
    instead of leaving `record` to fail on every later write.
    """
    last = None
    for _ in range(3):
        with db.connect() as c:
            if exists(c):
                return
        try:
            with db.connect() as c:
                apply(c)
        except Exception as exc:
            # Another process got there first (or is mid-commit): re-check on a
            # fresh transaction, which blocks until the winner is done.
            last = exc
            continue
        # "It did not raise" is not the same as "it is there". A statement that
        # silently no-ops — `IF NOT EXISTS` matching something that is not what
        # this module needs — would otherwise be recorded as success and wedge
        # the process, since the memo above never asks again.
        with db.connect() as c:
            if exists(c):
                return
        last = last or RuntimeError(
            "schema statement reported success but the object is still absent")
    try:
        with db.connect() as c:
            if exists(c):
                return
    except Exception:
        # The catalog read failing must not replace the DDL error that caused
        # all this — that is the one a reader needs to see.
        log.exception("Could not re-check the inbound_rejects schema")
    raise last


def _ensure_schema() -> None:
    """Put the table, column and indexes in place — once per process, never in a write.

    This deliberately does *not* run per connection, the pattern the rest of the
    app uses. `record` runs its upsert and `_prune` inside one transaction, and
    DDL sharing that transaction is a live lead-loss window on Postgres: a
    concurrent `CREATE INDEX IF NOT EXISTS` takes a ShareLock on the table *even
    when the index already exists*, which conflicts with the RowExclusiveLock
    another request's upsert holds. Measured on PostgreSQL 16 with 4 concurrent
    processes in the steady state where every object already exists — a
    permanent condition, not a deploy-window one — `record()` raised
    `DeadlockDetected` on 5 to 17 of every 32 calls depending on the run, and
    24/32 at cold start. Every one of those is a lead dropped behind a 202,
    because the ingress swallows it. With the DDL hoisted out and nothing else
    changed: 0/32, every run. Putting a single `CREATE INDEX IF NOT EXISTS` back
    onto the connection `_conn` returns brings it straight back (19/32), which is
    what attributes the difference to this and not to anything else.

    So the request path gets a plain connection, and in the steady state the
    cost of this is a memo lookup: after the first call, no DDL statement is
    issued at all — not even an idempotent one.
    """
    global _schema_ready

    want = _db_identity()
    if _schema_ready == want:
        return
    with _schema_lock:
        if _schema_ready == want:
            return
        # Every statement stays a literal at its `.execute()` call: the
        # portability lint reads them out of the AST, and SQL routed through a
        # variable is SQL it cannot check.
        _ensure_object(_table_exists, lambda c: c.execute(
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
            mail_date TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            resolved_at TEXT,
            resolved_item_id TEXT
        )"""))
        _ensure_object(_has_mail_date, _add_mail_date)
        # Carries the dedup: an identical replay lands on the same row. Also the
        # conflict target of `record`'s upsert, so it must exist before any write.
        _ensure_object(
            lambda c: _index_exists(c, "ix_inbound_rejects_fp", unique=True),
            lambda c: c.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS ix_inbound_rejects_fp
                   ON inbound_rejects (tenant_id, site, fingerprint)"""),
        )
        # Every read here is tenant-scoped and the dashboard counts open rows on
        # each load, so the scoping columns carry their own index.
        _ensure_object(
            lambda c: _index_exists(c, "ix_inbound_rejects_tenant"),
            lambda c: c.execute(
                """CREATE INDEX IF NOT EXISTS ix_inbound_rejects_tenant
                   ON inbound_rejects (tenant_id, site, status)"""),
        )
        _schema_ready = want


def _conn() -> db.Conn:
    _ensure_schema()
    return db.connect()


# Phrases both engines use when the thing this module owns is not there. The
# last one is Postgres refusing an upsert whose conflict target is missing,
# i.e. the unique index gone while the table stayed.
_SCHEMA_GONE = (
    "no such table", "no such column", "does not exist",
    "matching the on conflict",
)


def _schema_may_be_gone(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(phrase in text for phrase in _SCHEMA_GONE)


def _repairs_schema(fn):
    """Re-check the schema once if a call says it has gone missing.

    The memo means this module asks about its table exactly once per process,
    which is the point — but it also removed a property the old per-connection
    `CREATE TABLE IF NOT EXISTS` had for free: recovery. Every other module here
    still self-heals if its table is dropped, the database is recreated, or a
    deploy rebuilds it under a running process; without this, that leaves this
    one module wedged for the process's whole lifetime while the rest of the app
    carries on. And wedged is expensive here: `record` failing is swallowed by
    the ingress behind a 202 (the silent loss this table exists to end) and
    `count_open` failing takes the dashboard down with it.

    One retry only, and only for errors that name a missing relation or column —
    a genuinely broken schema still surfaces rather than looping.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        global _schema_ready
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if not _schema_may_be_gone(exc):
                raise
            log.warning("inbound_rejects schema looks absent (%s); re-checking", exc)
            with _schema_lock:
                _schema_ready = None
            return fn(*args, **kwargs)
    return wrapper


def _row(r) -> dict:
    return dict(zip(_COLS, r))


def _fingerprint(subject: str, sender: str, body: str) -> str:
    raw = "\x00".join((subject, sender, body)).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()


# Which rows survive the cap, best first. Ordering by usefulness rather than by
# recency alone is load-bearing: a host who forwards *all* their mail instead of
# filtering on furnishedfinder.com produces `sender_not_allowed` rows at
# newsletter volume, and a plain newest-wins cap would let 200 of those silently
# delete the one genuine unreadable enquiry — the exact loss this table exists to
# prevent, reintroduced by its own bookkeeping. So: still-open before resolved,
# a lead we failed to read before mail that was never ours, and only then recent.
#
# `received_at` leads the recency tiebreak because a duplicate forward bumps that
# and not `id`; ordering by `id` alone would evict the freshest row first.
_KEEP_IDS = """SELECT id FROM inbound_rejects
                WHERE tenant_id=? AND site=?
                ORDER BY CASE WHEN status=? THEN 0 ELSE 1 END,
                         CASE WHEN reason_code=? THEN 0 ELSE 1 END,
                         received_at DESC, id DESC
                LIMIT ?"""

_KEEP_PARAMS = ("unparsed",)  # the code that must outlive everything else


def _prune(c: db.Conn, tenant_id: str, site: str) -> None:
    """Enforce the per-tenant row cap, keeping the most useful rows.

    Postgres has no `DELETE ... LIMIT`, so the bound goes in a subselect. This
    is the portable form; the SQLite-only shorthand passes tests locally and
    fails on the hosted database.
    """
    # Cheap guard first. Pruning means two `NOT IN` scans, and almost every
    # message arrives with the table nowhere near full — doing that work per
    # ingest put ~20ms of DB on a public endpoint's request thread.
    total = c.execute(
        "SELECT COUNT(*) FROM inbound_rejects WHERE tenant_id=? AND site=?",
        (tenant_id, site),
    ).fetchone()
    if not total or total[0] <= MAX_ROWS_PER_TENANT:
        return

    keep = (tenant_id, site, OPEN) + _KEEP_PARAMS + (MAX_ROWS_PER_TENANT,)

    # A cap that quietly discards evidence is indistinguishable from the bug.
    doomed = c.execute(
        f"SELECT COUNT(*) FROM inbound_rejects WHERE tenant_id=? AND site=? "
        f"AND status=? AND id NOT IN ({_KEEP_IDS})",
        (tenant_id, site, OPEN) + keep,
    ).fetchone()
    if doomed and doomed[0]:
        log.warning(
            "Pruning %s unreviewed rejected inbound row(s) for tenant %s at the "
            "%s-row cap — raise the cap or review sooner",
            doomed[0], tenant_id, MAX_ROWS_PER_TENANT,
        )

    c.execute(
        f"DELETE FROM inbound_rejects WHERE tenant_id=? AND site=? "
        f"AND id NOT IN ({_KEEP_IDS})",
        (tenant_id, site) + keep,
    )


@_repairs_schema
def record(tenant_id: str, site: str, reason_code: str, reason: str,
           payload: dict) -> int | None:
    """Store one rejected inbound message. Returns the row id.

    Callers must have already established that this rejection is worth keeping
    (see the module docstring); this function does not re-check authentication.
    """
    import inbound  # local: inbound imports nothing from here, keep it that way

    # The column is TEXT and every reader scopes on it, so coerce once here
    # rather than leave a tenant addressable as both 7 and "7".
    tenant_id = str(tenant_id)
    subject = inbound.extract_subject(payload)[:_MAX_SUBJECT]
    sender = inbound.extract_sender(payload)[:_MAX_SENDER]
    body = inbound.extract_body(payload)[:MAX_STORED_BODY]
    # The mail `Date`, kept because retry has to hand the parser the same stamp
    # the webhook did. A *message* id hashes it, so dropping it collapsed two
    # sends of the same words onto one deal — see the retry route.
    mail_date = inbound.extract_date(payload)[:_MAX_MAIL_DATE]
    fp = _fingerprint(subject, sender, body)
    now = _now()
    reason = (reason or _CAPTURE_REASON.get(reason_code) or "")[:_MAX_REASON]

    with _conn() as c:
        # An exact replay bumps the counter instead of adding a row. `status` is
        # deliberately NOT reset: a row the operator already dismissed or
        # recovered stays that way, otherwise Dismiss would be undone by the
        # next duplicate forward and the list could never be cleared.
        c.execute(
            """INSERT INTO inbound_rejects
                   (tenant_id, site, reason_code, reason, subject, sender, body,
                    fingerprint, seen_count, received_at, mail_date, status)
               VALUES (?,?,?,?,?,?,?,?,1,?,?,?)
               ON CONFLICT (tenant_id, site, fingerprint) DO UPDATE SET
                   seen_count = inbound_rejects.seen_count + 1,
                   received_at = excluded.received_at,
                   reason_code = excluded.reason_code,
                   reason = excluded.reason,
                   mail_date = COALESCE(NULLIF(excluded.mail_date, ''),
                                        inbound_rejects.mail_date)""",
            (tenant_id, site, reason_code, reason, subject,
             sender, body, fp, now, mail_date, OPEN),
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


@_repairs_schema
def open_for_tenant(tenant_id: str, site: str) -> list[dict]:
    """Unresolved rejections, newest first — what the review page lists."""
    tenant_id = str(tenant_id)
    with _conn() as c:
        rows = c.execute(
            f"{_SELECT} WHERE tenant_id=? AND site=? AND status=? ORDER BY id DESC",
            (tenant_id, site, OPEN),
        ).fetchall()
    return [_row(r) for r in rows]


@_repairs_schema
def count_open(tenant_id: str, site: str) -> int:
    """How many leads are currently sitting unread. Drives the dashboard banner."""
    tenant_id = str(tenant_id)
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM inbound_rejects WHERE tenant_id=? AND site=? AND status=?",
            (tenant_id, site, OPEN),
        ).fetchone()
    return int(row[0]) if row else 0


@_repairs_schema
def count_all(tenant_id: str, site: str) -> int:
    """Every retained row, resolved or not — the denominator on the settings line."""
    tenant_id = str(tenant_id)
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM inbound_rejects WHERE tenant_id=? AND site=?",
            (tenant_id, site),
        ).fetchone()
    return int(row[0]) if row else 0


@_repairs_schema
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


@_repairs_schema
def dismiss(tenant_id: str, site: str, rid: int) -> bool:
    """Take a row off the open list, keeping it as an audit record."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE inbound_rejects SET status=?, resolved_at=? "
            "WHERE id=? AND tenant_id=? AND site=? AND status=?",
            (DISMISSED, _now(), rid, str(tenant_id), site, OPEN),
        )
        return bool(cur.rowcount)


@_repairs_schema
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


@_repairs_schema
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


@_repairs_schema
def update_reason(tenant_id: str, site: str, rid: int, reason: str) -> None:
    """Refresh the failure detail after a retry that still could not parse."""
    with _conn() as c:
        c.execute(
            "UPDATE inbound_rejects SET reason=? WHERE id=? AND tenant_id=? AND site=?",
            ((reason or "")[:_MAX_REASON], rid, str(tenant_id), site),
        )
