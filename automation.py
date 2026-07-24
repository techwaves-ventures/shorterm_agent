"""The agent loop: decide what's due, draft it, and route it for sending.

This ties the three new pieces together —

    pipeline.py   what stage each deal is in and when its next touch is due
    sequences.py  what that touch should say and whether it may send unattended
    outbox.py     where the drafted message waits for approval or sending

— and is the only module that runs the model on a schedule. It is deliberately
re-runnable: `run_due()` can be called from a worker loop, a cron, or a request
handler, and duplicate work is prevented by `outbox.has_open_step`, not by
assuming it runs exactly once.

Nothing here touches a browser. Drafting is pure API work; the actual send is
drained separately (see `send_next`) because platform replies drive real Chrome
one at a time.
"""
import logging
import threading
import time

import config
import outbox
import pipeline
import responder
import sequences
import storage

log = logging.getLogger(__name__)


def settings_for(tenant_id: str) -> dict:
    """This tenant's automation posture: master switch + auto-send-eligible steps."""
    s = config.get_settings(tenant_id)
    raw = s.get("auto_steps")
    if raw is None:
        allowed = sequences.default_enabled_steps()
    else:
        allowed = {x.strip() for x in str(raw).split(",") if x.strip()}
    return {
        "enabled": str(s.get("automation_enabled")) in ("1", "true", "True"),
        "steps": allowed,
    }


def reschedule(tenant_id: str, site: str, item_id: str) -> dict | None:
    """Recompute a deal's next due step from its current sequence position."""
    deal = pipeline.get(tenant_id, site, item_id)
    if not deal:
        return None
    if deal.get("stage") in (pipeline.LOST, pipeline.COMPLETED):
        pipeline.update(tenant_id, site, item_id,
                        next_action_at=None, next_action_step=None)
        return pipeline.get(tenant_id, site, item_id)
    when, step_id = sequences.schedule(deal)
    pipeline.update(tenant_id, site, item_id,
                    next_action_at=when, next_action_step=step_id)
    return pipeline.get(tenant_id, site, item_id)


def after_contact(tenant_id: str, site: str, item_id: str) -> None:
    """Called when a message actually reaches the guest.

    Advances the deal past the step we just delivered and schedules the next
    one. This is what starts the follow-up clock — a deal only enters the
    nurture cadence once real contact has been made, so a draft the owner never
    approved never triggers follow-ups.
    """
    deal = pipeline.get(tenant_id, site, item_id)
    if not deal:
        return
    pipeline.record_contact(tenant_id, site, item_id)
    idx = int(deal.get("step_index") or 0)
    seq = deal.get("sequence")
    if not sequences.is_last_step(seq, idx):
        stage = deal.get("stage")
        fields = {"step_index": idx + 1}
        # A second pre-sale touch means we're formally nurturing, not just contacted.
        if seq == sequences.PRESALE and idx >= 1 and stage == pipeline.CONTACTED:
            fields["stage"] = pipeline.NURTURING
        pipeline.update(tenant_id, site, item_id, **fields)
        reschedule(tenant_id, site, item_id)
    else:
        pipeline.update(tenant_id, site, item_id,
                        next_action_at=None, next_action_step=None)


def start_prearrival(tenant_id: str, site: str, item_id: str,
                     check_in: str | None = None, check_out: str | None = None) -> None:
    """Owner marked the deal booked — hand it to the pre-arrival sequence."""
    pipeline.mark_booked(tenant_id, site, item_id, check_in, check_out)
    reschedule(tenant_id, site, item_id)


def _due(deal: dict, now_iso: str) -> bool:
    when = deal.get("next_action_at")
    return bool(when) and str(when) <= now_iso


def run_due(tenant_id: str, site: str, limit: int = 25) -> dict:
    """Draft every step that has come due for this tenant.

    Returns a summary: {drafted, auto_queued, skipped, errors}. Each drafted
    message lands in the outbox as `queued` (auto-send armed and permitted) or
    `pending_approval` (everything else).
    """
    from datetime import datetime

    now_iso = datetime.now().isoformat(timespec="seconds")
    auto = settings_for(tenant_id)
    units = config.get_units(tenant_id)
    summary = {"drafted": 0, "auto_queued": 0, "skipped": 0, "errors": 0}

    deals = [d for d in pipeline.all_deals(tenant_id, site) if _due(d, now_iso)]
    for deal in deals[:limit]:
        item_id = deal["item_id"]
        step = sequences.step_at(deal.get("sequence"),
                                 int(deal.get("step_index") or 0))
        if not step:
            pipeline.update(tenant_id, site, item_id,
                            next_action_at=None, next_action_step=None)
            continue

        # Already drafted/sent this exact step — just move the deal forward.
        if outbox.has_open_step(tenant_id, site, item_id, step["id"]):
            _advance(tenant_id, site, deal)
            continue

        item = storage.get_item(tenant_id, site, item_id)
        if not item:
            log.warning("Deal %s has no stored item; clearing schedule", item_id)
            pipeline.update(tenant_id, site, item_id,
                            next_action_at=None, next_action_step=None)
            continue

        try:
            drafted = responder.draft_step(
                item, tenant_id, deal, step, units=units,
                history=outbox.sent_bodies(tenant_id, site, item_id),
            )
        except Exception as exc:
            log.exception("Draft failed for %s step %s", item_id, step["id"])
            summary["errors"] += 1
            # Push the retry out an hour rather than hammering a failing API.
            pipeline.update(tenant_id, site, item_id,
                            next_action_at=_plus_hour(now_iso))
            continue

        if drafted.get("skip") or not (drafted.get("message") or "").strip():
            summary["skipped"] += 1
            _advance(tenant_id, site, deal)
            continue

        may_auto = auto["enabled"] and sequences.can_auto_send(step, auto["steps"])
        outbox.add(
            tenant_id, site, item_id,
            sequence=deal.get("sequence") or "",
            step_id=step["id"], step_label=step["label"],
            body=drafted["message"], auto=may_auto,
            reason=drafted.get("reason") or "",
            scheduled_at=deal.get("next_action_at"),
        )
        summary["drafted"] += 1
        if may_auto:
            summary["auto_queued"] += 1
        _advance(tenant_id, site, deal)

    return summary


def _advance(tenant_id: str, site: str, deal: dict) -> None:
    """Move a deal to the next step in its sequence (or end the sequence).

    Note this advances on *draft*, not on send: the schedule describes when the
    agent should have prepared each touch. Delivery timing is the outbox's job.
    """
    idx = int(deal.get("step_index") or 0)
    seq = deal.get("sequence")
    item_id = deal["item_id"]
    if sequences.is_last_step(seq, idx):
        pipeline.update(tenant_id, site, item_id,
                        next_action_at=None, next_action_step=None)
        return
    pipeline.update(tenant_id, site, item_id, step_index=idx + 1)
    reschedule(tenant_id, site, item_id)


def _plus_hour(now_iso: str) -> str:
    from datetime import datetime, timedelta

    return (datetime.fromisoformat(now_iso) + timedelta(hours=1)).isoformat(
        timespec="seconds"
    )


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def send_next(tenant_id: str, site: str, timeout: int = 300) -> dict | None:
    """Deliver the oldest queued message via the existing reply path.

    Kept separate from drafting because this drives a real browser: the runner
    serializes it behind the global run-lock, so messages drain one at a time.
    Returns the message that was dispatched, or None if the queue is empty.
    """
    import runner

    msg = outbox.next_queued(tenant_id)
    if not msg:
        return None
    item = storage.get_item(tenant_id, site, msg["item_id"])
    if not item:
        outbox.set_status(msg["id"], outbox.FAILED, error="stored item not found")
        return msg
    # Claim it before dispatching so a second drainer can't pick up the same row.
    outbox.set_status(msg["id"], outbox.SENDING)
    state = runner.send_reply(tenant_id, site, item, msg["body"])
    # A busy runner means another run owns the browser; put it back and retry later.
    if state.get("status") == "busy":
        outbox.set_status(msg["id"], outbox.QUEUED)
        return None

    # send_reply dispatches to a background thread and returns immediately, so
    # its return value says nothing about delivery. Wait for the run to reach a
    # terminal state before recording an outcome — otherwise a failed send is
    # stored as `sent` and the follow-up cadence advances on a message the guest
    # never received.
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = runner.get_state(tenant_id)
        if not snapshot.get("running"):
            if snapshot.get("status") == "error":
                error = snapshot.get("message") or "send failed"
                outbox.set_status(msg["id"], outbox.FAILED, error=error)
                _notify_failure(msg, error)
            else:
                outbox.set_status(msg["id"], outbox.SENT)
                after_contact(tenant_id, site, msg["item_id"])
            return msg
        time.sleep(2)

    outbox.set_status(msg["id"], outbox.FAILED,
                      error="timed out waiting for the send to finish")
    _notify_failure(msg, "the send timed out")
    return msg


def _notify_failure(msg: dict, reason: str) -> None:
    """Tell the operator a send failed, since they may have navigated away.

    The card shows the error too, but a failed reply to a real guest shouldn't
    depend on someone happening to look at the right screen.
    """
    try:
        from notify import notify

        notify(
            "Reply failed to send",
            f"{msg.get('step_label') or 'Reply'} for this guest didn't go out: {reason}",
        )
    except Exception:
        log.exception("Could not send failure notification")


# ---------------------------------------------------------------------------
# Background drainer (in-process, for hosts without a separate worker)
# ---------------------------------------------------------------------------

_drain_lock = threading.Lock()
_draining = False

# How long the drainer keeps looking for work before parking itself. Long enough
# to ride out a scrape holding the browser, short enough not to linger forever.
_IDLE_ROUNDS = 6
_IDLE_SLEEP = 5


def start_drainer(site: str) -> bool:
    """Ensure a background thread is draining the send queue. Idempotent.

    Sending drives a real browser and takes tens of seconds, so it must never
    happen on the request thread — the user clicks send and gets their UI back
    immediately, while this delivers and records the outcome.
    """
    global _draining
    with _drain_lock:
        if _draining:
            return False
        _draining = True
    threading.Thread(
        target=_drain_loop, args=(site,), name="outbox-drainer", daemon=True
    ).start()
    return True


def _drain_loop(site: str) -> None:
    global _draining
    try:
        idle = 0
        while idle < _IDLE_ROUNDS:
            tenants = outbox.queued_tenants()
            if not tenants:
                idle += 1
                time.sleep(_IDLE_SLEEP)
                continue
            progressed = False
            for tenant_id in tenants:
                try:
                    if send_next(tenant_id, site) is not None:
                        progressed = True
                except Exception:
                    log.exception("Drainer failed for tenant %s", tenant_id)
            if progressed:
                idle = 0
            else:
                # Queue non-empty but nothing moved — the browser is busy with a
                # scrape. Back off rather than spin.
                idle += 1
                time.sleep(_IDLE_SLEEP)
    finally:
        with _drain_lock:
            _draining = False


# ---------------------------------------------------------------------------
# Autopilot: scheduled checks
# ---------------------------------------------------------------------------

_sched_lock = threading.Lock()
_scheduling = False
# How often to ask "is anyone owed a check?". The slot logic is idempotent, so
# this only controls how promptly a slot is noticed, not how often it fires.
_SCHED_POLL = 60


def run_scheduled_checks(site: str = "furnishedfinder") -> int:
    """Start an automatic scrape for every tenant currently owed one.

    Marks the slot as covered *before* dispatching: a scrape can take minutes
    (and may block on an OTP), and a second pass in the meantime must not launch
    a duplicate browser run for the same tenant.
    """
    import runner
    import scheduler

    started = 0
    for tenant_id in scheduler.due_tenants():
        try:
            scheduler.mark_checked(tenant_id)
            state = runner.start_scrape(tenant_id)
            if state.get("status") == "busy":
                log.info("Autopilot: tenant %s busy, will retry next slot", tenant_id)
                continue
            log.info("Autopilot: started scheduled check for tenant %s", tenant_id)
            started += 1
        except Exception:
            log.exception("Autopilot check failed for tenant %s", tenant_id)
    return started


def start_scheduler(site: str = "furnishedfinder") -> bool:
    """Run the autopilot schedule in-process (for hosts without a worker).

    Idempotent — only one scheduler thread per process.
    """
    global _scheduling
    with _sched_lock:
        if _scheduling:
            return False
        _scheduling = True
    threading.Thread(
        target=_scheduler_loop, args=(site,), name="autopilot-scheduler", daemon=True
    ).start()
    return True


def _scheduler_loop(site: str) -> None:
    global _scheduling
    try:
        while True:
            try:
                if run_scheduled_checks(site):
                    # A check just ran; give the drainer a chance to deliver
                    # anything it produced.
                    start_drainer(site)
            except Exception:
                log.exception("Autopilot scheduler pass failed")
            try:
                import digest

                digest.run_due()
            except Exception:
                log.exception("Daily digest pass failed")
            time.sleep(_SCHED_POLL)
    finally:
        with _sched_lock:
            _scheduling = False


def enqueue_send(tenant_id: str, site: str, item_id: str, body: str,
                 step_label: str = "Reply") -> dict | None:
    """Queue an approved reply for background delivery and kick the drainer.

    Used by the dashboard's send button: the message is recorded as `queued`
    (already approved by the click), so the request returns straight away and
    the card tracks delivery from the outbox.
    """
    deal = pipeline.get(tenant_id, site, item_id)
    step_id = (deal or {}).get("next_action_step") or "intro"
    msg = outbox.add(
        tenant_id, site, item_id,
        sequence=(deal or {}).get("sequence") or sequences.PRESALE,
        step_id=step_id, step_label=step_label, body=body,
        auto=True,  # the human just approved it by clicking send
        reason="Approved by you",
    )
    start_drainer(site)
    return msg
