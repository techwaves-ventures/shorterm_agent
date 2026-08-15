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
from datetime import datetime

import db

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

# The states a message may be *called off* from — everything except `sent`.
# Calling off a sent message is not a no-op: `sent_bodies()` selects exactly the
# `sent` rows, and it is the only duplicate-send guard on /responder/send. So
# cancelling one the guest has already read empties that history and re-arms the
# very second delivery the approve guard exists to stop. The cancel button sits
# on the same card as approve, so the stale-tab and back-button replays that
# guard was written for reach this one too.
CANCELABLE = (PENDING, QUEUED, SENDING, FAILED, CANCELED)

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


def _conn() -> db.Conn:
    c = db.connect()
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
    return c


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row(row) -> dict | None:
    return dict(zip(_COLS, row)) if row else None


def add(tenant_id: str, site: str, item_id: str, *, sequence: str, step_id: str,
        step_label: str, body: str, auto: bool, reason: str = "",
        scheduled_at: str | None = None) -> dict | None:
    """Queue a drafted step. `auto=True` skips the approval gate (goes straight
    to `queued`); otherwise it waits for a human in `pending_approval`."""
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
             body, status, 1 if auto else 0, reason, scheduled_at or now, now,
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
    """
    cutoff = datetime.now().timestamp() - max_age_seconds
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
                started = datetime.fromisoformat(
                    str(msg.get("sending_at") or "")).timestamp()
            except ValueError:
                # Unknown start time: assume it began now and revisit next pass.
                c.execute("UPDATE outbox SET sending_at=? WHERE id=?",
                          (_now(), msg["id"]))
                continue
            if started >= cutoff:
                continue
            if int(msg.get("attempts") or 0) >= MAX_SEND_ATTEMPTS:
                c.execute(
                    "UPDATE outbox SET status=?, error=? WHERE id=?",
                    (FAILED,
                     f"Abandoned after {MAX_SEND_ATTEMPTS} send attempts — "
                     "may already have reached the guest; check before retrying.",
                     msg["id"]),
                )
                continue
            c.execute("UPDATE outbox SET status=? WHERE id=?", (QUEUED, msg["id"]))
            requeued += 1
    return requeued


def next_queued(tenant_id: str | None = None,
                now_iso: str | None = None) -> dict | None:
    """The oldest message that is ready to send *now*.

    `scheduled_at` used to order the queue without gating it, so a send that had
    been deliberately pushed to a civilised hour went out the moment a drainer
    woke up — the quiet-hours clamp computed upstream had no effect on delivery
    at all. A message is due only once its scheduled time has arrived.
    """
    sql = f"{_SELECT} WHERE status=? AND scheduled_at<=?"
    params: list = [QUEUED, now_iso or _now()]
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
        sets.append("scheduled_at=CASE WHEN scheduled_at>? THEN ? ELSE scheduled_at END")
        vals += [_now(), _now()]
    if status == SENDING:
        # When the send actually started — which is what "stuck" is measured
        # against. `approved_at` cannot answer that: a message approved into
        # quiet hours sits queued for hours before anyone picks it up.
        sets.append("sending_at=?")
        vals.append(_now())
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

    A message outside `CANCELABLE` — meaning one already `sent` — is returned
    unchanged rather than cancelled, the same way `approve` refuses to release a
    row that has already reached the guest. Callers tell the two apart by the
    status of the row that comes back.
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
