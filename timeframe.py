"""The one frame for timestamps that two hosts compare.

Most timestamps here are naive local wall-clock, which is fine while a single
host both writes and reads them. The *schedule* columns are not that: on the
documented worker-queue topology (`Procfile`, `DEPLOY.md` — Vercel runs the
dashboard, `worker.py` runs elsewhere, both on one `DATABASE_URL`) a schedule is
written by whichever process drafts or approves the message and read by
whichever process drains it, and nothing pins either host's zone.

Comparing a naive stamp across that boundary is off by the offset, in both
damaging directions. Westward, an approved message is never "due" so it is
simply not delivered — and because the row is `queued` rather than `sending`,
`reclaim_stuck_sending` cannot rescue it and no operator sees a signal.
Eastward, a send deliberately pushed to a civilised hour is released early,
defeating the quiet-hours clamp the schedule exists to enforce.

So these columns carry one global frame: UTC. Two properties matter and both are
load-bearing:

  * **Naive, suffix-free.** `outbox.scheduled_at` and `deals.next_action_at` are
    compared with SQL `<=` and ordered with `ORDER BY` — that is a *lexicographic*
    comparison on text. An offset-aware string breaks it outright
    ('...T11:00:00+00:00' <= '...T05:00:00-07:00' is False lexically and True in
    absolute time), and `pipeline.norm_ts` would additionally rewrite any aware
    value back to reader-local. Same shape as before, one frame instead of many.
  * **UTC, not "the host's zone".** That is the whole point: the frame must not
    depend on which process happened to write the row.

Deliberately narrow. `_now()` in `outbox`/`pipeline` stays naive-local for
`approved_at`/`sent_at`/`created_at`/`last_contact_at` — those record when
something happened for a human to read, are never compared across hosts, and the
quiet-hours clamp is computed in local terms on purpose.

Two limits worth stating plainly rather than discovering later. The frame is
host-independent for schedules derived from *now*; a schedule derived from a
stored anchor inherits that anchor's undeclared zone (see `stamp`). And which
zone the quiet-hours clamp itself should compute in is a separate open question
(VEN-141) — this module fixes the frame a schedule is *stored and compared* in,
not the zone it is *decided* in.
"""
from datetime import datetime, timezone


def now() -> str:
    """The current instant, in the schedule frame."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def stamp(dt: datetime) -> str:
    """Convert a datetime into the schedule frame.

    A naive `dt` is a wall-clock reading on *this* host, so it is read in the
    host's local zone. `astimezone()` resolves that against the zone's real
    history rather than a cached offset, so a time on the far side of a DST
    boundary gets that day's offset, not today's.

    Know the limit of that. "Read it as this host's zone" is exactly right for a
    `now()`-derived time — the writer really was this host, at that instant. It
    is an *assumption* for a schedule derived from a stored anchor:
    `sequences.due_at` offsets from `check_in`/`last_contact_at`/`inquiry_at`,
    which are still naive host-local strings, so the same deal and step stamped
    on hosts in four zones lands on four different absolute instants. That is
    still strictly better than before — any two hosts previously disagreed,
    whereas now they agree whenever the host that wrote the anchor is the one
    rescheduling, which is the common case — but it means this module's
    "one frame, host-independent" guarantee is complete only for `now()`-derived
    schedules. Closing it fully means moving the anchor columns into a declared
    frame as well, deliberately not in scope here.
    """
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def parse(value) -> datetime | None:
    """A stored schedule stamp as an aware UTC datetime, or None if unreadable.

    Unreadable rather than raising: a stamp we cannot parse is a display problem,
    never a reason to fail a request.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace(" ", "T", 1))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def to_zone(value, tz=None) -> datetime | None:
    """A stored schedule stamp rendered in `tz` (host-local when None).

    Storage is UTC so that hosts agree; an operator still has to read the time
    they configured, so every human-facing render goes back through here.
    """
    dt = parse(value)
    if dt is None:
        return None
    return dt.astimezone(tz) if tz is not None else dt.astimezone()
