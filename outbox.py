"""Outbound message queue — everything the agent wants to say, and its approval state.

This is the single place a message lives between "the agent drafted it" and "it
reached the guest". Making it a durable table (rather than sending inline) buys
three things the product needs:

  * **A visible agent.** The dashboard can show work that is scheduled, awaiting
    approval, and already sent — which is what makes the automation legible
    instead of spooky.
  * **A real approval gate.** Human-in-the-loop is enforced by status, not by a
    UI convention: nothing reaches a guest without passing through `queued`.
  * **Serialized sending.** Platform replies drive a real browser one at a time,
    so sends have to drain from a queue regardless of how fast drafts appear.

Statuses:
    pending_approval -> queued -> sent
                     \\-> canceled          (human declined)
                        queued -> failed    (send error; retryable)
"""
from datetime import datetime, timezone

import db
import timeframe

PENDING = "pending_approval"
QUEUED = "queued"
SENDING = "sending"   # a browser send is in flight for this message
SENT = "sent"
FAILED = "failed"
CANCELED = "canceled"

OPEN_STATUSES = (PENDING, QUEUED, SENDING, FAILED)
# States the UI reports back on a card after the user hits send.
IN_FLIGHT = (QUEUED, SENDING)
# The only states a message may be *released to send* from: still waiting for a
# human, or a failure the human is retrying. Anything else has already reached
# the guest, is on its way to them, or was deliberately called off, and
# approving it again starts a second delivery. Enforced in `approve()` rather
# than in the route, because there are two approve buttons and only one of them
# had a guard — hiding a button is not a guard when a double-click, a stale tab
# or a back-button replay all re-POST the same approval.
APPROVABLE = (PENDING, FAILED)

# The states a message may be *called off* from — everything the guest has not
# already been written to.
#
# `sent` is excluded because calling off a sent message is not a no-op:
# `sent_bodies()` selects exactly the `sent` rows, and it is the only
# duplicate-send guard on /responder/send. So cancelling one the guest has
# already read empties that history and re-arms the very second delivery the
# approve guard exists to stop. The cancel button sits on the same card as
# approve, so the stale-tab and back-button replays that guard was written for
# reach this one too.
#
# `sending` is excluded for the same reason one step earlier. Nothing in
# `runner._send_worker` consults outbox status, so a drainer that has already
# claimed the row (`automation.py`, right before `send_reply`) writes to the
# guest regardless, and `send_next` then overwrites the row with the real
# outcome — `sent`, or `failed`, which is `APPROVABLE` and so hands a
# called-off message back as retryable. Accepting the cancel would report
# success for a message the guest receives anyway. An operator cannot stop an
# in-flight browser send; the honest answer is "too late". This strands
# nothing: a row wedged by a crashed process is returned to `queued` (or failed
# at the attempt cap) by `reclaim_stuck_sending`, which every dashboard render
# now calls unconditionally — it is pure DB work, and gating it on "can this
# process drive a browser" left the worker-queue topology unable to recover the
# rows its own ungated `start_drainer` had claimed. Both of those states are
# cancelable again.
#
# Accepted cost: for the first `reclaim_stuck_sending` interval (900s from
# `sending_at`) a crashed send is uncancelable, where before it was cancelable
# immediately. That window is the price of not lying about delivery — inside it
# we genuinely cannot tell a wedged send from a slow one, and the conservative
# read is the one that does not tell the guest's message it was called off
# while it is still going out.
CANCELABLE = (PENDING, QUEUED, FAILED, CANCELED)

# Human-readable status for the card line under a deal.
STATUS_LABELS = {
    PENDING: "Waiting for your approval",
    QUEUED: "Queued to send…",
    SENDING: "Sending…",
    SENT: "Sent",
    FAILED: "Send failed",
    CANCELED: "Canceled",
}

_COLS = (
    "id", "tenant_id", "site", "item_id", "sequence", "step_id", "step_label",
    "body", "status", "auto", "reason", "scheduled_at", "created_at",
    "approved_at", "sent_at", "error", "sending_at", "attempts",
)

# How many times a send may be reclaimed before we stop retrying it. Each
# reclaim is a message we cannot prove *didn't* reach the guest, so the cap is
# the difference between "recover from a crashed worker" and "message the guest
# forever". Failing loudly at the cap puts it in front of a human instead.
MAX_SEND_ATTEMPTS = 3

_SELECT = f"SELECT {', '.join(_COLS)} FROM outbox"


def _ddl(c) -> None:
    # NOTE (VEN-146/VEN-145): this block moved out of _conn(). VEN-145 adds a
    # `claim_token` column to BOTH the CREATE TABLE and the migration loop
    # below. Merging the two branches can resolve cleanly while dropping it, and
    # the send-ownership guard then fails *open* with a green suite. Whoever
    # merges must assert "claim_token" in db.table_columns(c, "outbox").
    c.execute(
        """CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            site TEXT NOT NULL,
            item_id TEXT NOT NULL,
            sequence TEXT,
            step_id TEXT,
            step_label TEXT,
            body TEXT,
            status TEXT NOT NULL DEFAULT 'pending_approval',
            auto INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            scheduled_at TEXT,
            created_at TEXT,
            approved_at TEXT,
            sent_at TEXT,
            error TEXT
        )"""
    )
    # Idempotent migration for databases created before send-attempt tracking.
    have = db.table_columns(c, "outbox")
    for col, decl in (("sending_at", "TEXT"),
                      ("attempts", "INTEGER NOT NULL DEFAULT 0")):
        if col not in have:
            c.execute(f"ALTER TABLE outbox ADD COLUMN {col} {decl}")


def _conn() -> db.Conn:
    return db.open_with_schema("outbox", _ddl)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _now_utc() -> str:
    """An *absolute* stamp, for the one column compared across processes.

    Every other timestamp here is naive local wall-clock, which is fine while a
    single host both writes and reads it. `sending_at` is not that column: it is
    written by whichever process claims the send and read by whoever reclaims,
    and on the worker-queue topology those are different hosts sharing one
    `DATABASE_URL` — the worker may sit in any timezone while the web dyno runs
    UTC. Comparing a naive stamp across that boundary is off by the offset, and
    in the dangerous direction it makes a one-second-old live send look hours
    stale and hands it to a second drainer, delivering the message twice.

    So this column carries its offset. Deliberately narrow: `_now()` is shared
    with `approved_at`/`sent_at`/`created_at`, which record when something
    happened for a human to read and are never compared across hosts.

    `scheduled_at` used to be on that list and should not have been — it is
    written by the drafting/approving host and gated by the draining host, the
    same split this stamp exists for. It is fixed separately rather than here,
    because it is compared with SQL `<=` and `ORDER BY` and so cannot carry an
    offset suffix without breaking that lexicographic compare. See `timeframe`.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(row) -> dict | None:
    return dict(zip(_COLS, row)) if row else None


def add(tenant_id: str, site: str, item_id: str, *, sequence: str, step_id: str,
        step_label: str, body: str, auto: bool, reason: str = "",
        scheduled_at: str | None = None) -> dict | None:
    """Queue a drafted step. `auto=True` skips the approval gate (goes straight
    to `queued`); otherwise it waits for a human in `pending_approval`.

    `scheduled_at` is in the schedule frame (see `timeframe`) — callers that
    compute a send time hand one in, and "no particular time" means now.
    """
    now = _now()
    status = QUEUED if auto else PENDING
    with _conn() as c:
        new_id = db.insert_returning_id(
            c,
            """INSERT INTO outbox (tenant_id, site, item_id, sequence, step_id,
                   step_label, body, status, auto, reason, scheduled_at,
                   created_at, approved_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(tenant_id), site, str(item_id), sequence, step_id, step_label,
             body, status, 1 if auto else 0, reason,
             scheduled_at or timeframe.now(), now,
             now if auto else None),
        )
    return get(new_id)


def get(msg_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute(f"{_SELECT} WHERE id=?", (msg_id,)).fetchone()
    return _row(row)


def for_tenant(tenant_id: str, site: str, statuses: tuple = OPEN_STATUSES) -> list[dict]:
    placeholders = ",".join("?" * len(statuses))
    with _conn() as c:
        rows = c.execute(
            f"{_SELECT} WHERE tenant_id=? AND site=? AND status IN ({placeholders}) "
            "ORDER BY scheduled_at ASC, id ASC",
            (str(tenant_id), site, *statuses),
        ).fetchall()
    return [_row(r) for r in rows]


def pending_for_item(tenant_id: str, site: str, item_id: str) -> dict | None:
    """The message awaiting approval for this deal, if any."""
    with _conn() as c:
        row = c.execute(
            f"{_SELECT} WHERE tenant_id=? AND site=? AND item_id=? AND status=? "
            "ORDER BY id DESC LIMIT 1",
            (str(tenant_id), site, str(item_id), PENDING),
        ).fetchone()
    return _row(row)


def has_open_step(tenant_id: str, site: str, item_id: str, step_id: str) -> bool:
    """Whether this exact step is already drafted/queued/sent for this deal.

    The scheduler is deliberately re-runnable, so this is what stops a guest
    receiving the same follow-up twice.
    """
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM outbox WHERE tenant_id=? AND site=? AND item_id=? "
            "AND step_id=? AND status <> ? LIMIT 1",
            (str(tenant_id), site, str(item_id), step_id, CANCELED),
        ).fetchone()
    return row is not None


def sent_bodies(tenant_id: str, site: str, item_id: str) -> list[str]:
    """What we've already said to this guest — fed back to the model as history."""
    with _conn() as c:
        rows = c.execute(
            "SELECT body FROM outbox WHERE tenant_id=? AND site=? AND item_id=? "
            "AND status=? ORDER BY id ASC",
            (str(tenant_id), site, str(item_id), SENT),
        ).fetchall()
    return [r[0] for r in rows if r[0]]


def latest_by_item(tenant_id: str, site: str) -> dict[str, dict]:
    """The most recent outbox row per item — the per-card send state the UI polls.

    Ordered ascending so the last write for an item wins, giving each card its
    current state (queued / sending / sent / failed) without an N+1 lookup.
    """
    out: dict[str, dict] = {}
    with _conn() as c:
        rows = c.execute(
            f"{_SELECT} WHERE tenant_id=? AND site=? ORDER BY id ASC",
            (str(tenant_id), site),
        ).fetchall()
    for r in rows:
        msg = _row(r)
        if msg:
            out[msg["item_id"]] = msg
    return out


def queued_tenants() -> list[str]:
    """Tenants with at least one message cleared for delivery."""
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT tenant_id FROM outbox WHERE status=?", (QUEUED,)
        ).fetchall()
    return [str(r[0]) for r in rows]


def reclaim_stuck_sending(max_age_seconds: int = 900) -> int:
    """Requeue messages left in `sending` by a crashed process.

    Without this a process that dies mid-send strands the message forever: it is
    no longer `queued` so no drainer picks it up, but it never reached the guest.

    The danger is the mirror image — requeueing a send that is merely *slow*
    delivers the message twice — so this is deliberately conservative on both
    counts. Age is measured from `sending_at` (when a drainer actually picked
    the message up), never from `approved_at`: approval can precede the send by
    hours whenever the quiet-hours clamp defers delivery, which made every such
    message eligible for reclaim the moment it went in flight, on every
    dashboard load, forever. A row with no `sending_at` is treated as *just
    started* rather than infinitely old, so an unreadable stamp cannot trigger a
    duplicate. And `MAX_SEND_ATTEMPTS` bounds the loop: past the cap the message
    fails visibly instead of being re-sent without limit.

    The comparison is absolute, not wall-clock: the claiming process and the
    reclaiming one are different hosts on the worker-queue topology, and a naive
    stamp read across that boundary is off by the offset — westward it makes a
    live send look stale and delivers it twice. See `_now_utc`. A stamp that
    predates that column carrying its offset is therefore not judged at all,
    only replaced; ages are measured on the pass after.
    """
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - max_age_seconds
    requeued = 0
    with _conn() as c:
        rows = c.execute(
            f"{_SELECT} WHERE status=?", (SENDING,)
        ).fetchall()
        for r in rows:
            msg = _row(r)
            if not msg:
                continue
            try:
                stamp = datetime.fromisoformat(str(msg.get("sending_at") or ""))
            except ValueError:
                # Unknown start time: assume it began now and revisit next pass.
                c.execute("UPDATE outbox SET sending_at=? WHERE id=? AND status=?",
                          (_now_utc(), msg["id"], SENDING))
                continue
            if stamp.tzinfo is None:
                # Written before `sending_at` carried its offset (see
                # `_now_utc`), so it names a wall-clock reading on an unknown
                # host. There is no sound way to place it on the timeline: read
                # as the reader's local zone, a writer to the west resolves
                # hours into the past, and a one-second-old live claim is
                # requeued into a second drainer while the first is still
                # driving the browser. Guarding only the future-dated case
                # catches the eastward half of that and leaves the westward
                # half — the half that duplicates the message — wide open.
                #
                # So decline to judge it at all: treat it exactly like an
                # unparseable stamp above. Restamp absolute and let the next
                # pass measure a real age. The cost is that a row already
                # wedged at deploy time takes two passes — after the restamp, the
                # row must age out again (up to max_age_seconds); the alternative
                # is sending a live message twice.
                c.execute("UPDATE outbox SET sending_at=? WHERE id=? AND status=?",
                          (_now_utc(), msg["id"], SENDING))
                continue
            if stamp.timestamp() >= cutoff:
                continue
            # Every write below is guarded on the row still being `sending`. The
            # snapshot above was read before these run, so a send that reaches a
            # terminal state in the window between would otherwise be clobbered
            # back to `queued` — leaving `status='queued'` with `sent_at` set,
            # which also drops it out of `sent_bodies()` and disarms the
            # duplicate-send guard on /responder/send.
            if int(msg.get("attempts") or 0) >= MAX_SEND_ATTEMPTS:
                c.execute(
                    "UPDATE outbox SET status=?, error=? WHERE id=? AND status=?",
                    (FAILED,
                     f"Abandoned after {MAX_SEND_ATTEMPTS} send attempts — "
                     "may already have reached the guest; check before retrying.",
                     msg["id"], SENDING),
                )
                continue
            cur = c.execute("UPDATE outbox SET status=? WHERE id=? AND status=?",
                            (QUEUED, msg["id"], SENDING))
            requeued += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return requeued


def next_queued(tenant_id: str | None = None,
                now_iso: str | None = None) -> dict | None:
    """The oldest message that is ready to send *now*.

    `scheduled_at` used to order the queue without gating it, so a send that had
    been deliberately pushed to a civilised hour went out the moment a drainer
    woke up — the quiet-hours clamp computed upstream had no effect on delivery
    at all. A message is due only once its scheduled time has arrived.

    "Now" is absolute, not this host's wall clock: the drainer is routinely a
    different host from the one that scheduled the message, and a naive compare
    across that boundary withholds a due send for the whole offset (westward) or
    releases a deferred one early (eastward). See `timeframe`.
    """
    sql = f"{_SELECT} WHERE status=? AND scheduled_at<=?"
    params: list = [QUEUED, now_iso or timeframe.now()]
    if tenant_id is not None:
        sql += " AND tenant_id=?"
        params.append(str(tenant_id))
    sql += " ORDER BY scheduled_at ASC, id ASC LIMIT 1"
    with _conn() as c:
        row = c.execute(sql, params).fetchone()
    return _row(row)


def set_status(msg_id: int, status: str, *, error: str | None = None,
               body: str | None = None) -> None:
    sets = ["status=?"]
    vals: list = [status]
    if status == QUEUED:
        sets.append("approved_at=?")
        vals.append(_now())
        # Releasing a message means send it, so pull a future send time forward.
        # Every route into QUEUED is an explicit "go now" — a human approving, a
        # human retrying a failure, or reclaiming one stranded mid-flight — and
        # delivery now waits for scheduled_at. Leaving tomorrow morning's stamp
        # in place would make the operator click Send, see success, and watch
        # nothing happen for hours. GREATEST-style clamp, so a message that is
        # already due keeps its position and can't jump the queue.
        # Both sides in the schedule frame: the stored stamp was written by
        # whichever host drafted the message, and the approver is often another.
        sets.append("scheduled_at=CASE WHEN scheduled_at>? THEN ? ELSE scheduled_at END")
        _release = timeframe.now()
        vals += [_release, _release]
    if status == SENDING:
        # When the send actually started — which is what "stuck" is measured
        # against. `approved_at` cannot answer that: a message approved into
        # quiet hours sits queued for hours before anyone picks it up.
        sets.append("sending_at=?")
        vals.append(_now_utc())  # absolute: read by other hosts, see `_now_utc`
        sets.append("attempts=COALESCE(attempts,0)+1")
    if status == SENT:
        sets.append("sent_at=?")
        vals.append(_now())
    if error is not None:
        sets.append("error=?")
        vals.append(error[:400])
    if body is not None:
        sets.append("body=?")
        vals.append(body)
    vals.append(msg_id)
    with _conn() as c:
        c.execute(f"UPDATE outbox SET {', '.join(sets)} WHERE id=?", vals)


def approve(msg_id: int, body: str | None = None) -> dict | None:
    """Human approved (optionally after editing the text) — release it to send.

    `set_status` pulls a future `scheduled_at` forward, so approving a message
    the agent had scheduled for tomorrow morning sends it now.

    A message outside `APPROVABLE` is returned unchanged rather than released:
    re-approving a `sent` row put a second copy of a message the guest had
    already read back on the queue, and `next_queued` duly served it again.
    Callers tell the two apart by the status of the row that comes back.
    """
    msg = get(msg_id)
    if not msg or msg["status"] not in APPROVABLE:
        return msg
    set_status(msg_id, QUEUED, body=body)
    return get(msg_id)


def release_unattempted(msg_id: int) -> None:
    """Return a claimed message to the queue without charging it an attempt.

    A `busy` runner means another run owns the browser, so this message was
    never dispatched: nothing reached the guest and nothing was risked. The
    claim still has to increment `attempts` — that counter is what bounds a
    *crashed* send — so the busy path gives it back.

    Without this, `_drain_loop`'s six retries against a scrape holding the
    browser for ~30s burned through MAX_SEND_ATTEMPTS with zero deliveries, and
    the message was then abandoned on its first genuine stall with an
    operator-facing error saying it "may already have reached the guest" — which
    was false; it had been sent zero times.

    Only a row still in `SENDING` is released, because that is the claim being
    given back. Without the status check this re-queued whatever the row had
    since become — and an operator's cancel landing while the drainer held the
    claim was silently undone, delivering a message a human had explicitly
    called off. That race is reachable on every collision now that a same-tenant
    one is busy too, and `_drain_loop` retries it every few seconds.
    """
    with _conn() as c:
        c.execute(
            "UPDATE outbox SET status=?, sending_at=NULL, "
            "attempts=CASE WHEN COALESCE(attempts,0)>0 THEN attempts-1 ELSE 0 END "
            "WHERE id=? AND status=?",
            (QUEUED, msg_id, SENDING),
        )


def cancel(msg_id: int) -> dict | None:
    """Human called the message off before it went out.

    A message outside `CANCELABLE` — one already `sent`, or one a drainer has
    claimed and is `sending` — is returned unchanged rather than cancelled, the
    same way `approve` refuses to release a row that has already reached the
    guest. In both of those states the guest is written to whatever this
    returns, so reporting a cancel would be a lie. Callers tell the two apart by
    the status of the row that comes back.
    """
    msg = get(msg_id)
    if not msg or msg["status"] not in CANCELABLE:
        return msg
    set_status(msg_id, CANCELED)
    return get(msg_id)


def counts(tenant_id: str, site: str) -> dict:
    with _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) FROM outbox WHERE tenant_id=? AND site=? "
            "GROUP BY status",
            (str(tenant_id), site),
        ).fetchall()
    by_status = {r[0]: r[1] for r in rows}
    return {
        "pending": by_status.get(PENDING, 0),
        "queued": by_status.get(QUEUED, 0),
        "sent": by_status.get(SENT, 0),
        "failed": by_status.get(FAILED, 0),
    }
