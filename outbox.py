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
    "approved_at", "sent_at", "error",
)

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
                started = datetime.fromisoformat(str(msg.get("approved_at") or "")).timestamp()
            except ValueError:
                started = 0
            if started < cutoff:
                c.execute("UPDATE outbox SET status=? WHERE id=?", (QUEUED, msg["id"]))
                requeued += 1
    return requeued


def next_queued(tenant_id: str | None = None) -> dict | None:
    """The oldest message ready to send (optionally scoped to one tenant)."""
    sql = f"{_SELECT} WHERE status=?"
    params: list = [QUEUED]
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
    """Human approved (optionally after editing the text) — release it to send."""
    set_status(msg_id, QUEUED, body=body)
    return get(msg_id)


def cancel(msg_id: int) -> None:
    set_status(msg_id, CANCELED)


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
