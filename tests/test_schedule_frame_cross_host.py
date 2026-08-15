"""VEN-134 — a schedule written by one host must mean the same thing on another.

The worker-queue topology (`Procfile`, `DEPLOY.md`) puts the dashboard and
`worker.py` on different hosts sharing one `DATABASE_URL`, and nothing pins
either host's timezone. Every other test in this suite writes and reads inside a
single process, so the whole suite is TZ-invariant and structurally blind to a
frame mismatch: it passes identically on `TZ=UTC` and `TZ=Pacific/Auckland`.

These tests move the *clock* between the write and the read, which is the only
way the defect is visible. They deliberately use nothing but production entry
points, so the file runs unchanged on the pre-fix head — where it fails.
"""
import os
import time as time_mod
from contextlib import contextmanager
from datetime import datetime, time as time_cls, timedelta, timezone as tz_utc_mod
from zoneinfo import ZoneInfo

tz_utc = tz_utc_mod.utc

import pytest

import automation
import config
import outbox
import pipeline
import responder
import sequences
import storage
import timeframe

SITE = "furnishedfinder"

# Spread west and east of UTC, and across two DST regimes so a fixed-offset
# shortcut cannot pass by accident.
ZONES = ("UTC", "America/New_York", "America/Los_Angeles", "Pacific/Auckland")


@contextmanager
def host_tz(name: str):
    """Run the block as if this process were a host in `name`.

    Yields the offset actually in effect. Callers assert on it: an unknown zone
    makes `TZ` degrade *silently* to UTC, and a control that never moved scores a
    perfect pass against unfixed code — which is exactly how this class of bug
    gets declared fixed while still shipping.
    """
    prev = os.environ.get("TZ")
    os.environ["TZ"] = name
    time_mod.tzset()
    try:
        yield datetime.now().astimezone().utcoffset()
    finally:
        if prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev
        time_mod.tzset()


@pytest.fixture()
def no_quiet_hours(monkeypatch):
    """Take the quiet-hours clamp out of the picture.

    Without this the tests are only as good as the hour they run at: when the
    clamp defers a stamp to tomorrow morning it is uniformly not-due on every
    host, so a frame mismatch is invisible and the suite goes green on broken
    code at some times of day and red at others. Widening the window keeps
    `next_send_time` an honest "the writer's clock, right now" so what is under
    test is the *frame*, not the clamp. Which zone the clamp itself resolves in
    is VEN-141, deliberately a separate question.
    """
    monkeypatch.setattr(sequences, "QUIET_START", time_cls(0, 0))
    monkeypatch.setattr(sequences, "QUIET_END", time_cls(23, 59))


@pytest.fixture()
def db_tenant(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "frame.db")
    tid = "1"
    config.save_settings(tid, host_name="Test Host", automation_enabled="1")
    yield tid
    # tzset is process-global; never leak a zone into the rest of the suite.
    os.environ.pop("TZ", None)
    time_mod.tzset()


def test_host_tz_helper_actually_moves_the_clock():
    """Positive control for the control. If this fails, every matrix below is
    measuring nothing and its green result is meaningless."""
    seen = {}
    for zone in ZONES:
        with host_tz(zone) as offset:
            assert offset is not None, f"{zone}: no offset in effect"
            seen[zone] = offset
    assert len(set(seen.values())) == len(ZONES), \
        f"zones collapsed onto the same offset — TZ is not taking effect: {seen}"
    assert seen["America/Los_Angeles"] < seen["America/New_York"] < seen["Pacific/Auckland"]


def test_frame_conversion_uses_the_offset_in_force_on_that_date():
    """A schedule can be months out, so the conversion must consult the zone's
    history rather than reuse today's offset.

    Capturing an offset once (`dt.astimezone().tzinfo`) and reusing it is the
    tempting shortcut, and it silently stamps a send an hour off across a DST
    boundary — into the quiet hours the clamp exists to protect. Asserting the
    January/July offsets differ is what makes that shortcut fail here.
    """
    import timeframe

    with host_tz("America/New_York") as offset:
        assert offset is not None
        winter = datetime(2026, 1, 15, 12, 0, 0)
        summer = datetime(2026, 7, 15, 12, 0, 0)
        winter_utc = datetime.fromisoformat(timeframe.stamp(winter))
        summer_utc = datetime.fromisoformat(timeframe.stamp(summer))

        assert winter_utc - winter == timedelta(hours=5), "EST is UTC-5"
        assert summer_utc - summer == timedelta(hours=4), "EDT is UTC-4"
        # And the wall clock an operator sees must survive the round trip.
        for wall in (winter, summer):
            back = timeframe.to_zone(timeframe.stamp(wall)).replace(tzinfo=None)
            assert back == wall, f"{wall} did not round-trip (got {back})"


def _queue(tenant, item_id, stamp):
    """Queue an auto-send message due at `stamp`, as autopilot does."""
    return outbox.add(tenant, SITE, item_id, sequence="presale", step_id="intro",
                      step_label="First reply", body="hi", auto=True,
                      reason="test", scheduled_at=stamp)


@pytest.mark.parametrize("writer_tz", ZONES)
def test_delivery_gate_agrees_across_reader_hosts(db_tenant, no_quiet_hours, writer_tz):
    """`outbox.next_queued` must reach the same verdict on every draining host.

    Westward this is a message that is due and simply never goes out; the row
    stays `queued`, so `reclaim_stuck_sending` cannot rescue it and no operator
    sees a signal. Eastward it is a message released before its scheduled hour.
    """
    with host_tz(writer_tz) as offset:
        assert offset is not None
        due_now = sequences.next_send_time()
        deferred = sequences.next_send_time(datetime.now() + timedelta(hours=2))

    # Separate tenants: `next_queued` returns only the single oldest due message,
    # so the two schedules have to be asked about independently.
    now_tenant, held_tenant = f"{db_tenant}-now", f"{db_tenant}-held"
    _queue(now_tenant, "due-now", due_now)
    _queue(held_tenant, "deferred", deferred)

    released, held = {}, {}
    for reader_tz in ZONES:
        with host_tz(reader_tz) as offset:
            assert offset is not None
            released[reader_tz] = bool(outbox.next_queued(now_tenant))
            held[reader_tz] = not outbox.next_queued(held_tenant)

    assert len(set(released.values())) == 1, (
        f"writer={writer_tz} scheduled {due_now}; drainers disagree on whether "
        f"it may go out: {released}")
    # The deferred send must be held by every reader, not just the writer's own
    # host — this gate is the quiet-hours clamp's only enforcement point.
    assert all(held.values()), (
        f"writer={writer_tz} deferred a send to {deferred}; released early by "
        f"{[z for z, ok in held.items() if not ok]}")


@pytest.mark.parametrize("writer_tz", ZONES)
def test_worker_work_list_agrees_across_reader_hosts(db_tenant, no_quiet_hours, writer_tz):
    """`pipeline.tenants_with_due` is the worker's entire work list.

    A westward worker that never sees the tenant never drafts the step at all —
    there is no outbox row and nothing on the dashboard, so this failure is even
    quieter than an undelivered message.
    """
    pipeline.ensure(db_tenant, SITE, {"id": "L1", "title": "lead"}, {}, [])
    with host_tz(writer_tz) as offset:
        assert offset is not None
        due_now = sequences.next_send_time()
    pipeline.update(db_tenant, SITE, "L1",
                    next_action_at=due_now, next_action_step="intro")

    verdicts = {}
    for reader_tz in ZONES:
        with host_tz(reader_tz) as offset:
            assert offset is not None
            verdicts[reader_tz] = db_tenant in pipeline.tenants_with_due()
    assert len(set(verdicts.values())) == 1, (
        f"writer={writer_tz} scheduled {due_now}; workers disagree on whether "
        f"the deal is due: {verdicts}")


@pytest.mark.parametrize("writer_tz", ZONES)
def test_approve_and_send_releases_on_every_reader_host(db_tenant, no_quiet_hours,
                                                        writer_tz):
    """The ticket's headline flow: a host clicks Approve & send on the dashboard
    and a worker elsewhere is supposed to deliver it.

    Approving pulls a future `scheduled_at` forward to "now" — every route into
    QUEUED is an explicit go-now. If that release stamp is the approving host's
    wall clock, a westward drainer still reads it as future and the operator
    watches a message they were told was sent go nowhere for hours.
    """
    msg = _queue(db_tenant, "held", "2099-01-01T09:00:00")
    with host_tz(writer_tz) as offset:
        assert offset is not None
        outbox.set_status(msg["id"], outbox.QUEUED)

    released = {}
    for reader_tz in ZONES:
        with host_tz(reader_tz) as offset:
            assert offset is not None
            picked = outbox.next_queued(db_tenant)
            released[reader_tz] = bool(picked and picked["id"] == msg["id"])
    assert all(released.values()), (
        f"approved on {writer_tz} (release stamp "
        f"{outbox.get(msg['id'])['scheduled_at']}); still withheld by "
        f"{[z for z, ok in released.items() if not ok]}")


@pytest.mark.parametrize("writer_tz", ZONES)
def test_send_now_with_no_explicit_schedule_agrees_across_hosts(db_tenant, writer_tz):
    """`automation.enqueue_send` (the dashboard's own Send button) queues without
    naming a time, so `outbox.add` supplies one. That default is a write like any
    other and has to be in the same frame as the gate that reads it."""
    with host_tz(writer_tz) as offset:
        assert offset is not None
        msg = outbox.add(db_tenant, SITE, "adhoc", sequence="presale",
                         step_id="intro", step_label="First reply", body="hi",
                         auto=True, reason="test")  # no scheduled_at

    released = {}
    for reader_tz in ZONES:
        with host_tz(reader_tz) as offset:
            assert offset is not None
            picked = outbox.next_queued(db_tenant)
            released[reader_tz] = bool(picked and picked["id"] == msg["id"])
    assert all(released.values()), (
        f"queued on {writer_tz} with no explicit time "
        f"({outbox.get(msg['id'])['scheduled_at']}); withheld by "
        f"{[z for z, ok in released.items() if not ok]}")


@pytest.mark.parametrize("writer_tz", ZONES)
def test_sequence_step_schedule_agrees_across_reader_hosts(db_tenant, no_quiet_hours,
                                                           writer_tz):
    """The follow-up cadence goes through `sequences.due_at`, not
    `next_send_time` — a different writer, the same frame requirement.

    `automation.reschedule` is the real entry point, so this covers the path a
    deal actually takes after each contact.
    """
    pipeline.ensure(db_tenant, SITE, {"id": "L1", "title": "lead"}, {}, [])

    with host_tz(writer_tz) as offset:
        assert offset is not None
        # The contact happened on this host, so its stamp is this host's wall
        # clock — the coherent case, and the one production actually produces.
        anchor = datetime.now().replace(microsecond=0) - timedelta(hours=49)
        pipeline.update(db_tenant, SITE, "L1", sequence="presale", step_index=1,
                        last_contact_at=anchor.isoformat(timespec="seconds"))
        automation.reschedule(db_tenant, SITE, "L1")
        # Oracle built from stdlib alone, not from the module under test:
        # followup_1 fires 48h after contact, and `astimezone()` resolves that
        # wall clock against the writer's real zone.
        expected = ((anchor + timedelta(hours=48))
                    .astimezone(tz_utc).replace(tzinfo=None))

    scheduled = pipeline.get(db_tenant, SITE, "L1")["next_action_at"]
    assert scheduled, "reschedule must have produced a due time"
    # Consistency alone is not enough here: once every reader shares one clock
    # they agree happily on a wrong instant, so pin the instant itself.
    assert datetime.fromisoformat(scheduled) == expected, (
        f"writer={writer_tz} stored {scheduled} for a step due at {expected} "
        f"absolute — the schedule was written in the wrong frame")

    verdicts = {}
    for reader_tz in ZONES:
        with host_tz(reader_tz) as offset:
            assert offset is not None
            verdicts[reader_tz] = db_tenant in pipeline.tenants_with_due()
    assert len(set(verdicts.values())) == 1, (
        f"writer={writer_tz} scheduled followup_1 at {scheduled}; workers "
        f"disagree on whether it is due: {verdicts}")


@pytest.mark.parametrize("writer_tz", ZONES)
def test_run_due_drafts_on_every_reader_host(db_tenant, no_quiet_hours, writer_tz, monkeypatch):
    """The second gate: `tenants_with_due` picks the tenant, `automation._due`
    then re-checks each deal. Leaving that one in host-local frame makes the
    worker wake up for a tenant and draft nothing, so it has to move too."""
    pipeline.ensure(db_tenant, SITE, {"id": "L1", "title": "lead"}, {}, [])
    with host_tz(writer_tz) as offset:
        assert offset is not None
        due_now = sequences.next_send_time()
    pipeline.update(db_tenant, SITE, "L1", sequence="presale", step_index=0,
                    next_action_at=due_now, next_action_step="intro")

    monkeypatch.setattr(storage, "get_item",
                        lambda *a, **k: {"id": "L1", "title": "lead"})
    monkeypatch.setattr(responder, "draft_step",
                        lambda *a, **k: {"message": "hello", "reason": "test"})

    drafted = {}
    for reader_tz in ZONES:
        # A fresh tenant per reader: `run_due` advances the deal and
        # `has_open_step` suppresses a repeat draft, so replaying on one tenant
        # would make every reader after the first look wrong for the wrong
        # reason. Each starts from the identical position.
        reader_tenant = f"{db_tenant}-{reader_tz.replace('/', '_')}"
        pipeline.ensure(reader_tenant, SITE, {"id": "L1", "title": "lead"}, {}, [])
        pipeline.update(reader_tenant, SITE, "L1", sequence="presale", step_index=0,
                        next_action_at=due_now, next_action_step="intro")
        config.save_settings(reader_tenant, host_name="Test Host",
                             automation_enabled="1")
        with host_tz(reader_tz) as offset:
            assert offset is not None
            drafted[reader_tz] = automation.run_due(reader_tenant, SITE)["drafted"]
    assert len(set(drafted.values())) == 1, (
        f"writer={writer_tz} scheduled {due_now}; run_due drafts on some hosts "
        f"and not others: {drafted}")


# --- the zone the clamp computes in (VEN-141, pinned here so it cannot drift) --


@pytest.mark.xfail(
    strict=True,
    reason="VEN-141: _clamp_quiet_hours computes in the server's zone, not the "
           "property's, so a send clamped to 08:00 on the dyno lands at 01:00 "
           "where the guest is. Strict: this must go red the day VEN-141 lands.",
)
def test_quiet_hours_clamp_uses_the_property_zone():
    """Quiet hours are a claim about the wall clock the *guest* reads.

    Every other test in this file takes the clamp out of the picture with
    `no_quiet_hours` so that what is under test is the frame, which leaves the
    file structurally unable to ask this question — and the host-zone assertion
    in `test_agent_lifecycle.py` round-trips by construction, so it cannot ask it
    either. Without this test the property-zone gap is asserted nowhere.

    VEN-134 moved *storage* to one absolute frame and deliberately left the clamp
    a local computation; deciding which local zone that is belongs to VEN-141.
    The consequence is visible in the dashboard today: `sched_local` renders in
    the property zone, so a send the clamp believes is safely at 08:00 displays
    as 01:00 — the product contradicting its own "an automated 3am message reads
    as a bot" promise (`sequences.py`).

    The host zone is pinned rather than inherited: on a host that happens to run
    in the property's own zone the clamp is accidentally right, this would XPASS,
    and a strict xfail would then report a green suite as a failure for a reason
    that has nothing to do with the defect.
    """
    prop = ZoneInfo("America/Los_Angeles")
    with host_tz("UTC") as offset:
        assert offset == timedelta(0), "host zone must be pinned for this to mean anything"
        # 03:00 where the property is, expressed as the host wall clock that
        # `next_send_time` actually takes. On 2026-09-01 LA is PDT (UTC-7), so
        # this is 10:00 on the dyno — already "daytime" to a server-zone clamp,
        # which is exactly why it sails through unclamped.
        three_am_at_the_property = datetime(2026, 9, 1, 3, 0, tzinfo=prop)
        as_the_host_reads_it = three_am_at_the_property.astimezone().replace(tzinfo=None)
        scheduled = sequences.next_send_time(as_the_host_reads_it)

    at_the_property = timeframe.to_zone(scheduled, prop)
    assert sequences.QUIET_START.hour <= at_the_property.hour <= sequences.QUIET_END.hour, (
        f"a send at 03:00 property-local was stored as {scheduled}Z, which is "
        f"{at_the_property:%H:%M} where the guest is — the clamp ran in the "
        f"server's zone")


def test_norm_ts_leaves_schedule_frame_stamps_alone():
    """`pipeline.norm_ts` must not rewrite a schedule-frame stamp to local time.

    `norm_ts` decides whether a value needs a UTC->local conversion from its
    *shape* — a space separator and no "T" means "database default, in UTC".
    `timeframe.now()` emits `isoformat()`, which uses "T", so today the heuristic
    correctly leaves it alone. Nothing states that as a requirement, though: it
    holds by coincidence of formatting, and `norm_ts` runs over `next_action_at`,
    the very column this ticket moved into the schedule frame.

    Switch `timeframe` to a space separator — a cosmetic-looking change — and
    every schedule stamp silently gets shifted by the reader's offset on the way
    out, which is precisely the bug VEN-134 removed. Pin it on both sides of UTC
    so the assertion cannot pass by the offset happening to be zero.
    """
    for zone in ZONES:
        with host_tz(zone) as offset:
            assert offset is not None
            stamped = timeframe.now()
            assert pipeline.norm_ts(stamped) == stamped, (
                f"norm_ts rewrote a schedule-frame stamp on a host in {zone}: "
                f"{stamped} -> {pipeline.norm_ts(stamped)}. The frame is UTC by "
                f"declaration, not by string shape.")
