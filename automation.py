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
import os
import threading
import time
from datetime import datetime, timedelta

import config
import outbox
import pipeline
import responder
import sequences
import storage
import timeframe

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


def after_contact(tenant_id: str, site: str, item_id: str,
                  at: str | None = None, once_since: str | None = None,
                  chase: bool = True) -> bool:
    """Called when a message actually reaches the guest.

    Advances the deal past the step we just delivered and schedules the next
    one. This is what starts the follow-up clock — a deal only enters the
    nurture cadence once real contact has been made, so a draft the owner never
    approved never triggers follow-ups.

    One UPDATE, deliberately. This used to be three (`record_contact`, the step
    bump, `reschedule`), and its caller swallows failures so that a lifecycle
    error cannot fail a reply the guest has already read. A fault between those
    writes therefore left the deal contacted-but-unadvanced with nothing raised
    to anyone — and, worse, in a state no later pass could recognise as broken,
    because "contact stamped" is exactly what a healthy advance looks like from
    outside. Folding them into one statement makes the advance all-or-nothing,
    which is what lets `reconcile_contacts` below identify an owed advance from
    the stored columns alone (VEN-219).

    `at` is when the message reached the guest (default now); the follow-up is
    anchored on it. `once_since` makes the write a compare-and-set against a
    contact stamp at or after that moment — pass the delivery time and two
    racing callers can only advance the deal once between them. Returns whether
    this call is what advanced the deal.

    `chase=False` records the contact and advances the step but leaves the deal
    stood down: no next action, no nurture promotion. It is for a repair of a
    delivery the guest has *already* written back to (see `reconcile_contacts`),
    and it is what `record_guest_reply` would have done to this deal the moment
    the reply arrived, had the advance not been lost. Advancing the step still
    matters: `step_index` names the *next* step to draft, so leaving it behind
    makes the owner's next touch re-send the one already delivered.
    """
    deal = pipeline.get(tenant_id, site, item_id)
    if not deal:
        return False
    fields = pipeline.contact_fields(deal, at)
    idx = int(deal.get("step_index") or 0)
    seq = deal.get("sequence")
    stage = deal.get("stage")
    if not sequences.is_last_step(seq, idx):
        fields["step_index"] = idx + 1
        # A second pre-sale touch means we're formally nurturing, not just
        # contacted. Read from the pre-contact snapshot, as it always has: a
        # deal arriving here `new` is promoted to `contacted` by this same
        # write, and it is not nurturing anyone on the strength of one touch.
        if (chase and seq == sequences.PRESALE and idx >= 1
                and stage == pipeline.CONTACTED):
            fields["stage"] = pipeline.NURTURING
        if not chase or stage in (pipeline.LOST, pipeline.COMPLETED):
            # `reschedule` refused to schedule a closed deal; keep that refusal,
            # since the merged write no longer goes through it. `chase=False`
            # lands in the same place for a different reason — the guest has
            # already answered, and nurturing means chasing silence.
            fields["next_action_at"] = None
            fields["next_action_step"] = None
        else:
            # Scheduled off the merged deal, so `followup_1` anchors on the
            # contact stamp this same statement is about to write.
            when, step_id = sequences.schedule({**deal, **fields})
            fields["next_action_at"] = when
            fields["next_action_step"] = step_id
    else:
        fields["next_action_at"] = None
        fields["next_action_step"] = None
    return pipeline.update(tenant_id, site, item_id,
                           uncontacted_since=once_since, **fields)


def start_prearrival(tenant_id: str, site: str, item_id: str,
                     check_in: str | None = None, check_out: str | None = None) -> None:
    """Owner marked the deal booked — hand it to the pre-arrival sequence."""
    pipeline.mark_booked(tenant_id, site, item_id, check_in, check_out)
    reschedule(tenant_id, site, item_id)


def _due(deal: dict, now_iso: str) -> bool:
    """Has the deal's next step come due? Both sides must be in the schedule
    frame (see `timeframe`) — this is a text compare, so a mismatched frame is
    silently off by the offset rather than an error."""
    when = deal.get("next_action_at")
    return bool(when) and str(when) <= now_iso


def run_due(tenant_id: str, site: str, limit: int = 25) -> dict:
    """Draft every step that has come due for this tenant.

    Returns a summary: {drafted, auto_queued, skipped, errors}. Each drafted
    message lands in the outbox as `queued` (auto-send armed and permitted) or
    `pending_approval` (everything else).
    """
    # Absolute, not host-local: this gates `next_action_at` (written by whichever
    # host last advanced the deal) and is also what the retry path writes back
    # via `_plus_hour`, so a local reading would drift the schedule every pass.
    now_iso = timeframe.now()
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


# Both failure branches in `send_next` can land on a message that really did
# reach the guest — the runner drives a browser, and it fails *after* the reply
# box as readily as before it. The card renders a primary "Retry send" beside
# whatever error is stored, so the text is the only thing standing between an
# operator and a second copy of the same message. Same register as the
# abandoned-send wording in `outbox`, from one constant so the two can't drift.
MAY_HAVE_REACHED_GUEST = "may already have reached the guest; check before retrying"


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
    # The claim has to be a compare-and-set for that sentence to be true:
    # `next_queued` is a separate read, so two drainers can both see `queued`,
    # and an unguarded write would let both dispatch. It would also silently
    # re-claim a row the operator cancelled in the gap. Losing the CAS means
    # someone else owns this row now — leave it alone.
    if not outbox.set_status(msg["id"], outbox.SENDING,
                             only_from=(outbox.QUEUED,)):
        return None
    state = runner.send_reply(tenant_id, site, item, msg["body"])
    # A busy runner means another run owns the browser; put it back and retry
    # later. Nothing was dispatched, so the claim's attempt is refunded — see
    # `release_unattempted`.
    if state.get("status") == "busy":
        outbox.release_unattempted(msg["id"])
        return None

    # send_reply dispatches to a background thread and returns immediately, so
    # its return value says nothing about delivery. Wait for the run to reach a
    # terminal state before recording an outcome — otherwise a failed send is
    # stored as `sent`, and the operator is never offered the retry that would
    # have got the message to the guest.
    #
    # This loop records the row's *outcome*; it does not advance the deal. The
    # worker does that, at the moment the reply actually lands — see
    # `runner._send_worker`. Doing both here fired `after_contact` twice per
    # delivered send (once from inside the worker, once here), and it is not
    # idempotent, so the deal jumped two steps and the guest was never sent
    # Followup 1.
    #
    # "The run" has to mean *this* dispatch. `runner._state` is one
    # process-global slot, so a not-running snapshot only ever proved that the
    # run which last touched that slot had finished. When this send's own
    # terminal state was overwritten before a 2-second poll saw it — by a
    # scrape, or by the next drained message — the loop read the replacement's
    # outcome as this row's, and a send that failed was recorded `sent`. The
    # token `send_reply` handed back is what distinguishes them.
    run_token = state.get("run_token")
    if not run_token:
        # Accepted-looking, but with no way to tell this run's outcome from
        # anyone else's. Fail closed: a wrongly-failed row is retried, while a
        # wrongly-sent one silently strands the guest and advances the cadence
        # past them.
        error = ("could not correlate the send run; delivery outcome unknown — "
                 f"{MAY_HAVE_REACHED_GUEST}")
        outbox.set_status(msg["id"], outbox.FAILED, error=error)
        _notify_failure(msg, error)
        return msg

    deadline = time.time() + timeout
    while time.time() < deadline:
        # This run's own recorded outcome first, the live slot only as a
        # fallback. The token stopped this loop adopting someone else's
        # outcome; it could not give this run back its own, because the slot it
        # was published to is global and the next run overwrites it. A scrape
        # claiming the runner inside this two-second gap left a genuinely
        # delivered send recorded `failed`. The record is keyed by this
        # dispatch's token, so by construction it can only ever answer with
        # this run's own outcome — the acceptance rule below is unchanged.
        outcome = runner.take_send_outcome(run_token, tenant_id)
        if outcome is None:
            snapshot = runner.get_state(tenant_id)
            # All three conditions, not just `not running`. A mismatched or
            # absent token belongs to someone else's run; an implicit status
            # like `idle` or `busy` is not an outcome this send ever reported.
            if (snapshot.get("run_token") == run_token
                    and not snapshot.get("running")
                    and snapshot.get("status") in ("done", "error")):
                outcome = snapshot
        if outcome is not None:
            if outcome.get("status") == "error":
                error = outcome.get("message") or "send failed"
                outbox.set_status(msg["id"], outbox.FAILED, error=error)
                _notify_failure(msg, error)
            else:
                outbox.set_status(msg["id"], outbox.SENT)
            return msg
        time.sleep(2)

    # Still not finished. Honest as a timeout — but a worker running past the
    # deadline may be running past it *inside* the reply it already delivered.
    error = f"timed out waiting for the send to finish — {MAY_HAVE_REACHED_GUEST}"
    outbox.set_status(msg["id"], outbox.FAILED, error=error)
    _notify_failure(msg, f"the send timed out — {MAY_HAVE_REACHED_GUEST}")
    return msg


# How long a delivery is left alone before a repair pass will touch it. The
# send worker stamps `sent_at` and *then* advances the deal, so for a moment
# every healthy send looks exactly like a stranded one. The compare-and-set in
# `after_contact` is what actually makes the repair safe; this only keeps the
# repair from racing a send that is still finishing, and keeps a cross-host
# clock skew (see `timeframe`) costing at most a delayed repair rather than a
# wrong one. Do not remove the CAS on the strength of this window existing.
SETTLE_SECONDS = 120


def _advance_owed(deal: dict, response: dict | None, before_iso: str) -> str | None:
    """The delivery time whose lifecycle advance never landed, or None.

    Derived, not recorded: `responses.sent_at` says a message reached the guest,
    `deals.last_contact_at` says the advance for it ran, and `after_contact`
    writes the second in one statement. So a contact stamp older than the send —
    or missing entirely — means the advance is owed, and the same predicate goes
    false the instant it is paid. That is the whole reason `after_contact` is one
    UPDATE; while it was three, a half-applied advance stamped the contact
    without advancing anything and this could not see it.
    """
    resp = response or {}
    if resp.get("status") != "sent":
        return None
    sent_at = pipeline.norm_ts(resp.get("sent_at"))
    if not sent_at or sent_at > before_iso:
        return None
    if pipeline.cmp_ts(deal.get("last_contact_at")) >= pipeline.cmp_ts(sent_at):
        return None
    return sent_at


def reconcile_contacts(tenant_id: str, site: str,
                       responses: dict[str, dict] | None = None) -> int:
    """Finish lifecycle advances that a delivered send failed to make.

    `runner._send_worker` swallows a failure here on purpose — a reply the guest
    has already read must not be reported as failed because the database was
    locked for a moment. The cost of that swallow is that the advance had
    exactly one chance, and losing it is silent: the reply goes out, the cadence
    never starts, and the board reads "awaiting guest" with nothing scheduled.
    Left alone, `pipeline.advance_lifecycle` then closes the deal as
    "No reply for 21 days" — a guest who was messaged once and never chased.

    So the swallow stays and this pays the debt afterwards, the way
    `outbox.reclaim_stuck_sending` pays for a send stranded mid-flight. It runs
    from the same two places for the same reason: the worker pass, and every
    dashboard render, because the default topology has no worker at all.

    A deal whose guest wrote back *after* the delivery being repaired is
    advanced but stood down — see `after_contact(chase=False)`. Chasing someone
    for silence they have already broken is the harm
    `pipeline.record_guest_reply` exists to prevent, and re-arming the cadence
    would also bury their reply, since "guest replied" is
    `last_guest_reply_at > last_contact_at`.

    That comparison is against `sent_at`, deliberately, and not against the
    row's own `last_contact_at` the way `lead_state` does. On a stranded
    deal `last_contact_at` is stale *by construction* — that staleness is the
    fault being repaired — so reading it answers "did the guest write after our
    last recorded contact" instead of "did they write after the delivery we are
    repairing". The two disagree on the ordinary ordering *guest writes in →
    owner replies → the advance strands*, which covers every send after the
    first on a deal the guest has answered, and getting it wrong there is not
    recoverable: standing the deal down stamps `last_contact_at = sent_at`, and
    that stamp is exactly what makes `_advance_owed` say nothing is owed. A
    guard inside a repair pass has to key off the repair's own reference value,
    never off the column the fault corrupted.
    """
    # Same shape and clock as `pipeline._now()`, which is what the two stamps
    # being compared are written in — deliberately not the schedule frame, since
    # `timeframe.stamp` would put the cutoff on a different clock to the values.
    before = (datetime.now() - timedelta(seconds=SETTLE_SECONDS)).isoformat(
        timespec="seconds")
    if responses is None:
        responses = storage.get_responses(tenant_id, site)
    repaired = 0
    for deal in pipeline.all_deals(tenant_id, site):
        item_id = deal["item_id"]
        try:
            sent_at = _advance_owed(deal, responses.get(item_id), before)
            if not sent_at:
                continue
            answered = (pipeline.cmp_ts(deal.get("last_guest_reply_at"))
                        > pipeline.cmp_ts(sent_at))
            if after_contact(tenant_id, site, item_id, at=sent_at,
                             once_since=sent_at, chase=not answered):
                repaired += 1
                log.warning(
                    "Recovered the lifecycle advance for %s: delivered at %s, "
                    "advance had not run%s", item_id, sent_at,
                    "; the guest has since replied, so no follow-up was armed"
                    if answered else "")
        except Exception:
            # One unusable deal must not stop the pass, and must not take the
            # dashboard render this runs inside down with it — the same posture
            # as `pipeline.backfill`'s per-item guard.
            log.exception("Could not reconcile the lifecycle advance for %s", item_id)
    return repaired


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
# Long-lived background threads
# ---------------------------------------------------------------------------


def background_agents_enabled() -> bool:
    """Whether this process may spawn the long-lived background threads.

    Consulted at the two spawn points below rather than at their callers: both
    are reachable from several places (`start_drainer` from six, including three
    request routes), so gating a caller only moves the leak to the next one.

    Default **on**, opt out with DISABLE_BACKGROUND_AGENTS=1. Every hosted
    entrypoint (`Procfile`, `render.yaml`, `Dockerfile`) starts the app by
    *importing* `dashboard`, so making the start explicit instead — the obvious
    alternative — would leave autopilot silently dead on every deploy that kept
    importing the module. The comment above `dashboard._start_background_agents()`
    records that exact failure happening once already. A default whose breakage
    is silent in production and invisible in CI is the wrong default even when
    it is the cleaner design; this direction makes a mis-set gate show up as a
    flaking suite, which tests can catch.

    The test suite opts out (`tests/conftest.py`): a daemon thread outlives the
    test that spawned it and keeps opening connections to whichever temp
    database is current, which made unrelated tests fail sporadically (VEN-162).
    """
    return os.getenv("DISABLE_BACKGROUND_AGENTS", "").strip().lower() not in (
        "1", "true", "yes",
    )


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
    # Checked before the latch, never after: a disabled call must not leave
    # `_draining` set, or the first enabled call would find itself suppressed.
    if not background_agents_enabled():
        return False
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
    # Before the latch, for the same reason as in `start_drainer`.
    if not background_agents_enabled():
        return False
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
    """Queue a **human-approved** reply for background delivery.

    Used by the dashboard's send button: the message is recorded as `queued`
    (already approved by the click), so the request returns straight away and
    the card tracks delivery from the outbox.

    Only call this when a person actually approved the text. Unattended sends go
    through `enqueue_autopilot_reply`, which records who authorized them.
    """
    deal = pipeline.get(tenant_id, site, item_id)
    step_id = (deal or {}).get("next_action_step") or "intro"
    msg = outbox.add(
        tenant_id, site, item_id,
        sequence=(deal or {}).get("sequence") or sequences.PRESALE,
        step_id=step_id, step_label=step_label, body=body,
        auto=True,  # the human just approved it by clicking send
        reason="Approved by you",
        # Returns None rather than stacking a second message onto a delivery
        # already under way. The caller's own "is anything in flight?" read
        # cannot carry that weight: two clicks both read "nothing" before either
        # inserted, and both inserted. Same rule as `outbox.release_to_send`,
        # insert-shaped instead of update-shaped.
        unless_in_flight=True,
        # And nothing rather than words this guest already has. The flag above
        # only sees a delivery still under way, so it is blind to a replay that
        # arrives once the first send has settled: a stale tab re-POSTs the text
        # it was opened with, and the caller's pre-read compares that against a
        # stored draft which has since moved on. Same rule, different axis — not
        # "two at once" but "the same words twice". See
        # `outbox._already_sent_terms`.
        unless_body_sent=True,
    )
    if msg is None:
        return None
    start_drainer(site)
    return msg


def enqueue_autopilot_reply(tenant_id: str, site: str, item_id: str, body: str,
                            reason: str = "",
                            step: dict | None = None) -> dict | None:
    """Queue autopilot's unattended first reply — through the same rails as the
    scheduled steps, and honestly labelled.

    This previously called `enqueue_send` directly, which meant an unattended
    message to a live prospect was written to the outbox as
    `reason="Approved by you"` with `approved_at` stamped, when no human had
    seen it. That is not a cosmetic string: the outbox is the audit record of
    who authorized contact with a guest, and it was recording a person where
    there was none. It also skipped `sequences.can_auto_send` and quiet hours,
    so the one path that sends without review was the one path with no rails.

    Now: the intro step must be auto-send-eligible under this tenant's settings
    (it is not, by default) or the message waits for approval like every other
    draft, and delivery is clamped out of the middle of the night.

    `step` names which step this is. It defaults to the intro because the common
    case is a brand-new lead, but a reply to a guest who wrote back must pass
    `sequences.GUEST_REPLY` — labelling that "First reply" told the operator the
    conversation was starting when it was already underway.
    """
    auto = settings_for(tenant_id)
    step = step or sequences.find_step(sequences.PRESALE, "intro") or {}
    may_auto = auto["enabled"] and sequences.can_auto_send(step, auto["steps"])
    deal = pipeline.get(tenant_id, site, item_id)
    msg = outbox.add(
        tenant_id, site, item_id,
        sequence=(deal or {}).get("sequence") or sequences.PRESALE,
        step_id=step.get("id") or "intro",
        step_label=step.get("label") or "First reply",
        body=body,
        auto=may_auto,
        reason=reason or ("Sent automatically by autopilot" if may_auto
                          else "Drafted by autopilot — awaiting your approval"),
        scheduled_at=sequences.next_send_time(),
    )
    if may_auto:
        start_drainer(site)
    return msg
