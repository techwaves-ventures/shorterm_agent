"""VEN-223: `advance_lifecycle` moves booked stages on the *property's* date.

`pipeline.advance_lifecycle` derived one `today` from `datetime.now()` — the
**server's** calendar date — and then compared it against `check_in` and
`check_out`, which are zoneless *property-calendar* dates: neither of their
write paths reads a clock (`pipeline.parse_date` normalising what the
FurnishedFinder listing says, or the operator typing into the booking form).
For the several hours a day the two zones disagree the worker therefore
**wrote** the wrong stage:

  * a guest was flipped to `staying` before they had arrived at the property,
    or left in `pre_arrival` on the day they did arrive;
  * a booking was closed to `completed` a day early — which also drops it out
    of `BOOKED_STAGES` and so off the arrivals list entirely;
  * `pre_arrival` fired off a horizon anchored to the wrong day.

This is the **write**-path twin of VEN-221 (`pipeline.arrivals`, a display
filter). The distinction is the whole reason this ticket is separate: VEN-221's
damage is undone by re-rendering, this one's is persisted, so a stage written on
the wrong day stays wrong.

## The trap this file exists to hold shut

The same `today` also derived the abandonment bound:

    stale_before = fromisoformat(today) - STALE_CLOSE_DAYS

and `_is_abandoned` compares that against `last_guest_reply_at` /
`last_contact_at` / `inquiry_at`. Re-pointing the single `today` at the property
fixes the three stage arms and breaks the abandonment arm by exactly the same
offset — a deal that is genuinely three weeks cold stops closing. So `today` had
to be **split** into two values, not moved, and one function now legitimately
holds two frames. Same rule as VEN-138, applied in the opposite direction:
compare each column in the frame it was written in.

`test_property_frame_today_does_not_move_the_abandonment_bound` and
`test_worker_call_path_keeps_the_abandonment_bound_in_the_server_frame` are the
two halves of that guard, and they are the tests that go red if a later change
collapses the two dates back together.

## And two of those three columns, not all three (VEN-225)

VEN-223 stated that all three abandonment columns are `pipeline._now()` server
stamps, and this docstring repeated it. It is true of only two:
`last_contact_at` and `last_guest_reply_at` reach the column through `update()`
-> `norm_ts`, whose contract is server-local. `inquiry_at` does not —
`pipeline.derive` writes `f"{listing_date}T09:00:00"` whenever the listing's date
parses, a zoneless **property**-calendar date, and falls back to `_now()` only
when it does not. `norm_ts` converts only space-separated values (the database's
UTC default), so a value that is already naive and `T`-separated passes the one
chokepoint built to enforce the frame.

Two frames in one column, with indistinguishable stored shapes, so no reader can
attribute a row to either. `advance_lifecycle` therefore carries a **third**
bound, `inquiry_stale_before = min(server_bound, property_bound)`, and
`_is_abandoned` compares each stamp against its own column's bound rather than
`max()`-ing across frames. `test_a_never_contacted_deal_is_not_closed_early_*`
and `test_the_server_framed_columns_keep_the_exact_bound` are the halves of
*that* guard.

## Why nothing here touches `TZ` / `tzset()`

That is process-global C state and poisons the rest of the run (PR #53 records
it breaking `test_review_fixes_5.py` about one run in four). Instead the
*server* instant is pinned by `monkeypatch`-ing the `datetime` each module
reads — an input to the code under test, never the function under test — and
the property zone is chosen relative to that pinned instant. Pinning the server
rather than waiting on the wall clock is also what makes **both** directions
reachable: at a given real hour only one of "property ahead" / "property
behind" exists, and a test that silently covers one direction reads as if it
covered both.

Standalone:

    ./.venv/bin/python -m pytest tests/test_lifecycle_property_frame.py -q
"""
import ast
import os
import subprocess
import sys
import tempfile
import textwrap
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO = str(Path(__file__).resolve().parent.parent)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("FF_CRED_KEY", "c9jwUi0L-fUjf3wjbq74M0lK3ah7fmEfGhjxZ7RehQk=")
os.environ.setdefault("SECRET_KEY", "test-secret")

import config  # noqa: E402
import pipeline  # noqa: E402
import scheduler  # noqa: E402
import storage  # noqa: E402

SITE = "furnishedfinder"

# Spans UTC-11 to UTC+14, so for any instant the scan below can reach a zone a
# day ahead of the server *and* one a day behind it.
CANDIDATE_ZONES = [
    "Pacific/Kiritimati",     # +14
    "Pacific/Auckland",       # +12/+13
    "Asia/Tokyo",             # +9
    "Asia/Kolkata",           # +5:30 (half-hour, so nothing assumes whole hours)
    "Europe/Berlin",          # +1/+2
    "UTC",                    # 0
    "America/New_York",       # -5/-4
    "America/Los_Angeles",    # -8/-7
    "Pacific/Honolulu",       # -10
    "Pacific/Midway",         # -11
]

# A fixed, DST-quiet reference day. Nothing in this file reads the real clock:
# every instant is constructed, so a run at 23:59 behaves like one at noon.
REF_DAY = date(2026, 3, 10)

AHEAD, BEHIND = 1, -1

# Behind every bound this file constructs, under any zone offset. `_open_deal`
# pins `inquiry_at` here so the inquiry arm can never be the deciding one.
INQUIRY_FAR_BEHIND = "2025-01-05T09:00:00"

# Read from the module rather than written as 21, so a change to the product
# rule moves these tests with it instead of quietly making them vacuous.
STALE = pipeline.STALE_CLOSE_DAYS


# --- clock and zone helpers -------------------------------------------------


class _FrozenClock:
    """A `datetime` drop-in pinned to one instant.

    A subclass, so `fromisoformat`, `combine`, arithmetic and `isinstance` all
    keep working and only the reading of *now* is replaced.
    """

    @staticmethod
    def make(frozen: datetime):
        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return frozen
                return frozen.astimezone().astimezone(tz)
        return _DT


def _freeze(monkeypatch, frozen: datetime, *modules):
    dt = _FrozenClock.make(frozen)
    for m in modules:
        monkeypatch.setattr(m, "datetime", dt)


def _zone_where_property_is(direction: int, day: date = REF_DAY):
    """`(zone, naive server instant, property date)` with the property's
    calendar date exactly `direction` days from the server's.

    Computed straight from `zoneinfo`, deliberately **not** from
    `scheduler.local_now`: a fixture that asked the code under test where
    midnight falls would move with the bug instead of pinning it. Every hour of
    the reference day is scanned, so this does not depend on what time the
    suite happens to run — which is the point (see the module docstring).
    """
    for zone in CANDIDATE_ZONES:
        tz = ZoneInfo(zone)
        for hour in range(24):
            for minute in (0, 30):
                naive = datetime.combine(day, time(hour, minute))
                # naive == server wall clock, so attach the runner's own zone
                # before converting rather than relying on an implicit one.
                prop = naive.astimezone().astimezone(tz)
                if (prop.date() - naive.date()).days == direction:
                    return zone, naive, prop.date()
    raise AssertionError(
        f"no candidate zone puts the property {direction:+d} day from the "
        f"server on {day}; the zone table has rotted and every assertion "
        "below would be vacuous")


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    """A clean tenant on its own database file, with **no** property timezone.

    Starting in the server frame means each test has to opt into a property
    zone explicitly, so it is always visible which frame an assertion is about.
    """
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    tid = "1"
    config.save_settings(tid, host_name="Test Host", timezone="",
                         digest_enabled="0", digest_hour="18:00",
                         autopilot="0", check_times="09:00,16:00",
                         last_check_at="", last_digest_at="")
    return tid


# --- deal helpers -----------------------------------------------------------


def _deal(tenant_id: str, item_id: str, guest: str) -> None:
    """An untouched `new` deal, with every timestamp column pinned far in the
    past so no test accidentally depends on when it was created."""
    item = {"id": item_id, "kind": "lead", "traveler": guest,
            "title": f"Unit 1 | Washington, District of Columbia | {guest}",
            "received_at": "January 5, 2026"}
    storage.filter_new(tenant_id, SITE, "lead", [item])
    pipeline.ensure(tenant_id, SITE, item, None)
    with pipeline._conn() as c:
        c.execute(
            "UPDATE deals SET created_at=?, inquiry_at=?, last_contact_at=NULL, "
            "last_guest_reply_at=NULL, next_action_at=NULL "
            "WHERE tenant_id=? AND site=? AND item_id=?",
            ("2026-01-05T09:00:00", "2026-01-05T09:00:00",
             str(tenant_id), SITE, item_id))


def _booked(tenant_id: str, item_id: str, guest: str,
            check_in: str | None = None, check_out: str | None = None) -> None:
    _deal(tenant_id, item_id, guest)
    pipeline.mark_booked(tenant_id, SITE, item_id,
                         check_in=check_in, check_out=check_out)


def _open_deal(tenant_id: str, item_id: str, guest: str, last_contact: str) -> None:
    """An exhausted open deal whose newest stamp is `last_contact`.

    `inquiry_at` is pushed far behind it on purpose, to 2025, so that every
    abandonment assertion built on this helper answers about `last_contact_at`
    and nothing else. That matters twice over now: `_is_abandoned` compares each
    column against its *own* bound (VEN-225), so an `inquiry_at` anywhere near
    either bound would let the inquiry arm decide the outcome and the test would
    quietly stop being about the column it names. 2025 is behind both bounds
    under every zone offset, so the inquiry arm always votes "stale" here and is
    never the deciding one.
    """
    _deal(tenant_id, item_id, guest)
    pipeline.update(tenant_id, SITE, item_id, stage=pipeline.CONTACTED,
                    last_contact_at=last_contact, next_action_at=None)
    with pipeline._conn() as c:
        c.execute(
            "UPDATE deals SET inquiry_at=? WHERE tenant_id=? AND site=? AND item_id=?",
            (INQUIRY_FAR_BEHIND, str(tenant_id), SITE, item_id))


def _untouched(tenant_id: str, item_id: str, listing_date: date) -> None:
    """A `new` deal created purely through the real write path — the exact
    opposite of `_open_deal`, and the population VEN-225 is about.

    Nothing is SQL-stamped afterwards, which is the whole point:
    `last_contact_at`, `last_guest_reply_at` and `next_action_at` are NULL
    because `ensure` never writes them, so `inquiry_at` is the newest — and
    only — stamp, and it is the one the abandonment decision turns on.

    `_open_deal` deliberately steers around this by pushing `inquiry_at` behind
    the contact stamp, which is precisely why VEN-223's ten tests could not
    reach the arm where a property-framed `inquiry_at` decides the outcome, and
    why this defect survived that PR. A fixture that avoids a column cannot
    fail on it.
    """
    item = {"id": item_id, "kind": "lead", "traveler": item_id,
            "title": f"Unit 1 | Washington, District of Columbia | {item_id}",
            "received_at": listing_date.strftime("%B %d, %Y")}
    storage.filter_new(tenant_id, SITE, "lead", [item])
    pipeline.ensure(tenant_id, SITE, item, None)


def _inquiry_only(tenant_id: str, item_id: str, inquiry_at: str) -> None:
    """An open deal whose only stamp is `inquiry_at`, set to the exact second.

    The write path can only ever store 09:00 (`derive` synthesises
    `f"{date}T09:00:00"`), so a boundary asserted to the second has to be
    stamped directly. That is sound here and only here: the shape the write path
    really produces is asserted separately, from the write path, by
    `test_inquiry_at_holds_two_frames_in_indistinguishable_shapes`.
    """
    _deal(tenant_id, item_id, item_id)
    with pipeline._conn() as c:
        c.execute(
            "UPDATE deals SET inquiry_at=? WHERE tenant_id=? AND site=? AND item_id=?",
            (inquiry_at, str(tenant_id), SITE, item_id))


def _inquiry_at(tenant_id: str, item_id: str) -> str:
    return pipeline.get(tenant_id, SITE, item_id)["inquiry_at"]


def _stage(tenant_id: str, item_id: str) -> str:
    return pipeline.get(tenant_id, SITE, item_id)["stage"]


def _stages(tenant_id: str) -> dict:
    return {d["item_id"]: d["stage"] for d in pipeline.all_deals(tenant_id, SITE)}


# --- the write path, through the exact call `worker.py` makes ---------------


def test_guest_who_has_arrived_at_the_property_is_moved_to_staying(tenant, monkeypatch):
    """AC1 — the filed defect, inverted, through `advance_lifecycle(tid, SITE)`.

    That bare two-argument call is verbatim what `worker.py` runs, so this is
    the test that kills the default-resolution mutant (`scheduler.local_now`
    swapped back for `datetime.now`). The ticket's "test the caller's argument
    too" note does not apply: the fix resolves the zone in the callee, because
    the caller already had `tenant_id` and still got the frame wrong.
    """
    zone, server_now, prop_date = _zone_where_property_is(AHEAD)
    assert prop_date != server_now.date(), ("vacuity guard", zone)

    config.save_settings(tenant, timezone=zone)
    _booked(tenant, "arrived", "Dana Reyes", check_in=prop_date.isoformat())
    # Positive control, same deal set: a booking a month out must not move, so
    # a pass cannot come from "advance_lifecycle moves everything".
    _booked(tenant, "far-off", "Kim Alvarez",
            check_in=(prop_date + timedelta(days=30)).isoformat())

    _freeze(monkeypatch, server_now, pipeline, scheduler)
    moved = pipeline.advance_lifecycle(tenant, SITE)

    assert _stage(tenant, "arrived") == pipeline.STAYING, (zone, moved)
    assert _stage(tenant, "far-off") == pipeline.BOOKED, (zone, moved)
    assert moved["staying"] == 1, moved


def test_guest_who_has_not_yet_arrived_at_the_property_is_left_alone(tenant, monkeypatch):
    """AC2 — the other direction: the property is a day *behind* the server.

    Both premature writes are asserted at once, each against a control on the
    same deal set, because an absence-only assertion passes just as well
    against a fix that never fires at all.
    """
    zone, server_now, prop_date = _zone_where_property_is(BEHIND)
    server_date = server_now.date()
    assert prop_date == server_date - timedelta(days=1), (zone, prop_date, server_date)

    config.save_settings(tenant, timezone=zone)
    # Arrives on the *server's* today, i.e. the property's tomorrow.
    _booked(tenant, "tomorrow", "Dana Reyes", check_in=server_date.isoformat())
    # Checks out on the property's today — the stay is not over yet.
    _booked(tenant, "checking-out", "Kim Alvarez",
            check_in=(prop_date - timedelta(days=5)).isoformat(),
            check_out=prop_date.isoformat())
    # Control: checked out on the property's *yesterday*, genuinely finished.
    _booked(tenant, "finished", "Lee Moreau",
            check_in=(prop_date - timedelta(days=6)).isoformat(),
            check_out=(prop_date - timedelta(days=1)).isoformat())

    _freeze(monkeypatch, server_now, pipeline, scheduler)
    moved = pipeline.advance_lifecycle(tenant, SITE)

    assert _stage(tenant, "tomorrow") == pipeline.PRE_ARRIVAL, (zone, moved)
    assert _stage(tenant, "checking-out") == pipeline.STAYING, (zone, moved)
    assert _stage(tenant, "finished") == pipeline.COMPLETED, (zone, moved)
    assert moved["staying"] == 1 and moved["completed"] == 1, moved


# --- the arms themselves, at the property's own boundaries ------------------


def test_checkin_and_checkout_boundaries_are_exact(tenant):
    """AC2's boundary half — `check_out <` and `check_in <=` are not off by one.

    Driven through the injected `today` rather than a zone, so it says
    something about the comparison itself and stays true whatever the runner's
    offset is. Four deals, two on each side of each boundary.
    """
    today = "2026-03-10"
    _booked(tenant, "out-today", "A", check_in="2026-03-01", check_out=today)
    _booked(tenant, "out-yesterday", "B", check_in="2026-03-01", check_out="2026-03-09")
    _booked(tenant, "in-today", "C", check_in=today)
    _booked(tenant, "in-tomorrow", "D", check_in="2026-03-11")

    pipeline.advance_lifecycle(tenant, SITE, today=today, server_today=today)
    stages = _stages(tenant)

    # A stay ends *after* the check-out date, so today's checkout is still a stay.
    assert stages["out-today"] == pipeline.STAYING, stages
    assert stages["out-yesterday"] == pipeline.COMPLETED, stages
    # Arrival day counts as arrived; the day before does not.
    assert stages["in-today"] == pipeline.STAYING, stages
    assert stages["in-tomorrow"] == pipeline.PRE_ARRIVAL, stages


def test_pre_arrival_horizon_moves_with_today(tenant, monkeypatch):
    """AC3 — `horizon` is derived from the **property's** `today`, inclusive at
    exactly `PRE_ARRIVAL_DAYS`.

    Two axes are varied, because each pins something the other cannot:

      * **two different `today` values**, which distinguishes a horizon derived
        from `today` from one re-read off the clock — a second `datetime.now()`
        cannot agree with both;
      * a `server_today` on a **different day** from `today`, and on the
        opposite side of it each time round. This is the axis the whole ticket
        is about and the one this test originally missed: it passed
        `server_today=today`, and two equal dates make the two frames
        indistinguishable, so nothing could say which of them `horizon` was
        anchored to. A horizon anchored to `server_today` survived the entire
        suite (review R1) — a guest arriving in exactly `PRE_ARRIVAL_DAYS`
        property-days is then never armed, the third harm the ticket names.

    Offsetting the server in both directions catches that at both boundaries: a
    server day *behind* drops `edge` out of a misanchored horizon, a server day
    *ahead* pulls `past` into it. `server_today` reaches nothing else here —
    these deals are booked, so the abandonment arm it feeds never runs.

    The clock is frozen to an instant unrelated to any of those dates so the
    re-read-the-clock mutant fails deterministically rather than depending on
    the day the suite runs.
    """
    _freeze(monkeypatch, datetime.combine(date(2026, 6, 1), time(12, 0)),
            pipeline, scheduler)

    for today, server_days_off in (("2026-03-10", -1), ("2026-04-19", +1)):
        base = date.fromisoformat(today)
        server_today = (base + timedelta(days=server_days_off)).isoformat()
        edge = (base + timedelta(days=pipeline.PRE_ARRIVAL_DAYS)).isoformat()
        past = (base + timedelta(days=pipeline.PRE_ARRIVAL_DAYS + 1)).isoformat()
        _booked(tenant, f"edge-{today}", "A", check_in=edge)
        _booked(tenant, f"past-{today}", "B", check_in=past)

        pipeline.advance_lifecycle(tenant, SITE, today=today,
                                   server_today=server_today)

        ctx = (today, server_today)
        assert _stage(tenant, f"edge-{today}") == pipeline.PRE_ARRIVAL, ctx
        assert _stage(tenant, f"past-{today}") == pipeline.BOOKED, ctx


# --- the trap: the abandonment bound must stay in the server frame ----------


def _abandonment_pair(tenant_id: str, server_now: datetime) -> None:
    """Two open deals straddling the server-frame `STALE_CLOSE_DAYS` bound by
    half a day each — close enough that a one-day shift of the bound flips the
    one that must not flip, far enough that nothing here is flaky."""
    midnight = datetime.combine(server_now.date(), time(0, 0))
    stale = midnight - timedelta(days=pipeline.STALE_CLOSE_DAYS)
    _open_deal(tenant_id, "cold", "Dana Reyes",
               (stale - timedelta(hours=12)).isoformat(timespec="seconds"))
    _open_deal(tenant_id, "warm", "Kim Alvarez",
               (stale + timedelta(hours=12)).isoformat(timespec="seconds"))


def test_property_frame_today_does_not_move_the_abandonment_bound(tenant, monkeypatch):
    """AC4 — a caller passing the property's date must not close deals early.

    This is the filed trap. `stale_before` is compared against server wall-clock
    stamps, so it must keep deriving from the server's date even when `today`
    is the property's. `cold` is the positive control: without it an assertion
    that `warm` survives would also pass against an abandonment arm that had
    simply stopped working.
    """
    zone, server_now, prop_date = _zone_where_property_is(AHEAD)
    config.save_settings(tenant, timezone=zone)
    _abandonment_pair(tenant, server_now)

    _freeze(monkeypatch, server_now, pipeline, scheduler)
    moved = pipeline.advance_lifecycle(tenant, SITE, today=prop_date.isoformat())

    assert _stage(tenant, "cold") == pipeline.LOST, (zone, moved)
    assert _stage(tenant, "warm") == pipeline.CONTACTED, (zone, moved)
    assert moved["lost"] == 1, moved


def test_worker_call_path_keeps_the_abandonment_bound_in_the_server_frame(
        tenant, monkeypatch):
    """AC4 via the default path — the same guard with **no** arguments passed.

    Distinct from the test above and not redundant with it: that one pins the
    explicit-`today` caller, this one pins how `server_today` *defaults*.
    Defaulting it from `scheduler.local_now` instead of `datetime.now()` would
    put both dates in the property frame and is invisible to any test that
    passes `today` itself.
    """
    zone, server_now, prop_date = _zone_where_property_is(AHEAD)
    assert prop_date != server_now.date(), ("vacuity guard", zone)

    config.save_settings(tenant, timezone=zone)
    _abandonment_pair(tenant, server_now)

    _freeze(monkeypatch, server_now, pipeline, scheduler)
    moved = pipeline.advance_lifecycle(tenant, SITE)

    assert _stage(tenant, "cold") == pipeline.LOST, (zone, moved)
    assert _stage(tenant, "warm") == pipeline.CONTACTED, (zone, moved)
    assert moved["lost"] == 1, moved


def test_abandonment_bound_is_anchored_to_midnight_not_to_the_hour(
        tenant, monkeypatch):
    """The comment above `stale_before` claims midnight anchoring; this holds it.

    `pipeline.py` says the bound is kept as `fromisoformat(server_today) - 21d`
    rather than re-spelled `datetime.now() - 21d`, "which would move the bound by
    up to a further 24h". Nothing asserted that: every other frozen instant in
    this file lands on midnight, where the two spellings agree, so the re-spelling
    survived the whole suite (review R5). Here the server is pinned at 13:45, so
    the misanchored bound reaches 13h45m further *forward* — `T00:00` becomes
    `T13:45` on the same day — and takes `warm` with it. (A later bound closes
    more deals, not fewer: staleness is `stamp < bound`.)

    Deliberately zone-free — the fixture tenant sets no timezone, so both dates
    fall back to the server frame and this says something about the *anchor*
    alone, not about any offset. It is green on `main` too, on purpose: midnight
    anchoring is behaviour this PR preserves rather than introduces, and a guard
    over preserved behaviour is supposed to be quiet until someone breaks it.
    """
    server_now = datetime.combine(REF_DAY, time(13, 45))
    assert server_now.time() != time(0, 0), "vacuity guard: midnight hides R5"

    # Straddles the *midnight-anchored* bound by 12h either side, so a bound
    # dragged forward by the 13h45m time-of-day flips `warm` and only `warm`.
    _abandonment_pair(tenant, server_now)

    _freeze(monkeypatch, server_now, pipeline, scheduler)
    moved = pipeline.advance_lifecycle(tenant, SITE)

    # `cold` is the positive control: it is past the bound under either
    # spelling, so "warm survived" cannot be an abandonment arm that never fired.
    assert _stage(tenant, "cold") == pipeline.LOST, moved
    assert _stage(tenant, "warm") == pipeline.CONTACTED, moved
    assert moved["lost"] == 1, moved


# --- VEN-225: the third column is not in the server frame at all ------------
#
# Everything above this line is about two frames. `inquiry_at` makes it three
# values in two frames inside one column, which is a different shape of problem:
# not "which frame is this comparison in" but "which frame is this *row* in",
# and that second question has no answer. The tests below pin what is left once
# it is admitted to be unanswerable.


def test_inquiry_at_holds_two_frames_in_indistinguishable_shapes(
        tenant, monkeypatch):
    """VEN-225 AC1 — the premise, updated by VEN-226.

    Rows written before VEN-226 hold two frames in shapes no reader can tell
    apart: `f"{listing_date}T09:00:00"` (a property-calendar date at 09:00) and
    `_now()` (a server instant). Both arrive naive, `T`-separated and to the
    second, so `norm_ts` passes both through untouched, and no reader downstream
    could tell which arm wrote a given row.

    After VEN-226 both arms write in the SERVER frame, so new rows no longer
    hold mixed frames. This test keeps its purpose — the core payload is the
    second half (both values are naive, T-separated, seconds precision, so
    `norm_ts` handles both correctly) — and now also asserts that the parsed arm
    stores 09:00 property-local *in the server frame*.
    """
    zone, server_now, prop_date = _zone_where_property_is(BEHIND)
    assert prop_date != server_now.date(), ("vacuity guard", zone)
    config.save_settings(tenant, timezone=zone)
    _freeze(monkeypatch, server_now, pipeline, scheduler)

    listing = prop_date - timedelta(days=STALE)
    _untouched(tenant, "parsed", listing)
    # The fallback arm: a `received_at` nothing can parse.
    unparseable = {"id": "fallback", "kind": "lead", "traveler": "u",
                   "title": "Unit 1 | Washington, District of Columbia | u",
                   "received_at": "not a date"}
    storage.filter_new(tenant, SITE, "lead", [unparseable])
    pipeline.ensure(tenant, SITE, unparseable, None)

    parsed, fallback = _inquiry_at(tenant, "parsed"), _inquiry_at(tenant, "fallback")
    # 09:00 on the listing's date, in the PROPERTY zone, expressed in the server frame.
    expected_parsed = scheduler.server_naive(
        tenant, datetime.combine(listing, time(9, 0))
    ).isoformat(timespec="seconds")
    assert parsed == expected_parsed, (zone, parsed, expected_parsed)
    assert fallback == server_now.isoformat(timespec="seconds"), fallback
    # Vacuity: the zone must be non-zero so the conversion produces a different
    # value from the bare "<date>T09:00:00" the old code produced.
    assert parsed != f"{listing.isoformat()}T09:00:00", (
        "vacuity guard: server_naive must differ from a bare 09:00 stamp — "
        "the zone is non-zero, so the conversion should change the time")

    # The fact `norm_ts` handles both: both are naive, T-separated, seconds precision.
    for value in (parsed, fallback):
        assert len(value) == 19 and value[10] == "T", value
        assert datetime.fromisoformat(value).microsecond == 0, value

    # And such a deal really is one where `inquiry_at` decides: it is open, and
    # the other two abandonment columns are NULL because `ensure` never wrote
    # them. This is what `_open_deal` steers around.
    row = pipeline.get(tenant, SITE, "parsed")
    assert row["stage"] in pipeline.OPEN_STAGES, row["stage"]
    assert row["last_contact_at"] is None, row
    assert row["last_guest_reply_at"] is None, row
    assert row["next_action_at"] is None, row


def test_a_never_contacted_deal_is_not_closed_early_in_the_property_frame(
        tenant, monkeypatch):
    """VEN-225 AC2 — the filed defect. **Red on VEN-223, on behaviour.**

    The property is a day behind the server, so the server-frame bound sits a
    day *later* than the property-frame one. A deal whose only stamp is a listing
    date `STALE_CLOSE_DAYS` property-days old is therefore past the server bound
    while not yet past its own, and the pre-fix code closed it — and `mark_lost`
    also clears `next_action_at`/`next_action_step`, so the sequence that might
    still have won the deal is cancelled with it. That is the harmful direction:
    the owner is shown a loss they did not take.

    `cold` is the positive control and `warm` the negative one, on the same deal
    set, because "edge survived" is equally true of an abandonment arm that has
    stopped firing at all.
    """
    zone, server_now, prop_date = _zone_where_property_is(BEHIND)
    assert prop_date == server_now.date() - timedelta(days=1), (zone, prop_date)
    config.save_settings(tenant, timezone=zone)

    # Ages measured in the PROPERTY's calendar — the frame the column is in.
    _untouched(tenant, "edge", prop_date - timedelta(days=STALE))
    _untouched(tenant, "cold", prop_date - timedelta(days=STALE + 1))
    _untouched(tenant, "warm", prop_date - timedelta(days=5))

    _freeze(monkeypatch, server_now, pipeline, scheduler)
    moved = pipeline.advance_lifecycle(tenant, SITE)

    ctx = (zone, server_now.date(), prop_date, moved)
    assert _stage(tenant, "cold") == pipeline.LOST, ("positive control", ctx)
    assert _stage(tenant, "warm") == pipeline.NEW, ("negative control", ctx)
    assert _stage(tenant, "edge") == pipeline.NEW, ctx
    assert moved["lost"] == 1, ctx


def test_the_server_framed_columns_keep_the_exact_bound(tenant, monkeypatch):
    """VEN-225 AC3 — the conservative bound is for `inquiry_at` **only**.

    Green on VEN-223, and the test that forbids the tempting simplification of
    handing the earlier bound to all three columns: `last_contact_at` and
    `last_guest_reply_at` genuinely are server stamps (`update()` -> `norm_ts`),
    so widening their bound would delay every genuinely-cold deal the rule
    exists to close, in exchange for nothing.

    The property must be a day *behind* here. Ahead, the earlier of the two
    bounds simply *is* the server bound, the two are the same value, and a
    collapse of the columns onto one bound is invisible — which is why the two
    VEN-223 guards above, both `AHEAD`, cannot catch it.
    """
    zone, server_now, prop_date = _zone_where_property_is(BEHIND)
    assert prop_date == server_now.date() - timedelta(days=1), (zone, prop_date)
    config.save_settings(tenant, timezone=zone)

    # `cold` 12h before the SERVER bound, `warm` 12h after it — so a bound
    # widened to the property frame (a further 24h back) flips `cold` and only
    # `cold`, and flipping it is the failure this asserts against.
    _abandonment_pair(tenant, server_now)

    # Vacuity guard on the fixture, not on the code. If `_open_deal` ever stops
    # pinning `inquiry_at` behind both bounds, the inquiry arm starts deciding
    # these deals and this test silently becomes a second copy of AC2.
    server_bound = (datetime.combine(server_now.date(), time(0, 0))
                    - timedelta(days=STALE))
    for item_id in ("cold", "warm"):
        assert _inquiry_at(tenant, item_id) < (
            server_bound - timedelta(days=2)).isoformat(timespec="seconds"), (
            "this test must answer about last_contact_at, not inquiry_at")

    _freeze(monkeypatch, server_now, pipeline, scheduler)
    moved = pipeline.advance_lifecycle(tenant, SITE)

    ctx = (zone, server_now.date(), prop_date, moved)
    assert _stage(tenant, "cold") == pipeline.LOST, ctx
    assert _stage(tenant, "warm") == pipeline.CONTACTED, ("positive control", ctx)
    assert moved["lost"] == 1, ctx


def test_each_bound_is_exact_to_the_second_for_its_own_column(tenant):
    """VEN-225 AC4 — both bounds, both sides, driven by explicit dates.

    No zone and no frozen clock: the two dates are passed in a day apart, which
    is all "the bounds differ" means, so this says something about the two
    comparisons themselves rather than about any offset. A stamp one second
    before its own bound closes; a stamp exactly on it does not — `<` not `<=`,
    preserved from the `max()` spelling this replaced.

    The pairing is the point. Each column is asserted against the bound that is
    *not* the other's, so swapping the two bounds over fails here even though
    every deal keeps a plausible-looking outcome.
    """
    today, server_today = "2026-03-09", "2026-03-10"       # property a day behind
    inquiry_bound = (datetime.fromisoformat(today) - timedelta(days=STALE)
                     ).isoformat(timespec="seconds")
    server_bound = (datetime.fromisoformat(server_today) - timedelta(days=STALE)
                    ).isoformat(timespec="seconds")
    assert inquiry_bound < server_bound, (inquiry_bound, server_bound)

    one_second = timedelta(seconds=1)
    _inquiry_only(tenant, "inq-on-bound", inquiry_bound)
    _inquiry_only(tenant, "inq-a-second-past",
                  (datetime.fromisoformat(inquiry_bound) - one_second
                   ).isoformat(timespec="seconds"))
    _open_deal(tenant, "con-on-bound", "A", server_bound)
    _open_deal(tenant, "con-a-second-past", "B",
               (datetime.fromisoformat(server_bound) - one_second
                ).isoformat(timespec="seconds"))

    moved = pipeline.advance_lifecycle(tenant, SITE, today=today,
                                       server_today=server_today)
    stages = _stages(tenant)

    assert stages["inq-on-bound"] == pipeline.NEW, stages
    assert stages["inq-a-second-past"] == pipeline.LOST, stages
    assert stages["con-on-bound"] == pipeline.CONTACTED, stages
    assert stages["con-a-second-past"] == pipeline.LOST, stages
    assert moved["lost"] == 2, (moved, stages)


def test_every_stamp_is_bounded_not_only_the_deciding_one(tenant):
    """VEN-225 AC8 — the rule is per *column*, not per deal (review R1).

    This change replaces an aggregate — `max()` of three stamps against one
    bound — with a per-element rule: each stamp against the bound for its own
    column. That restatement is **indistinguishable from the aggregate until two
    stamps straddle the two differing bounds**, and nothing in this file did
    that: `_open_deal` pins `inquiry_at` at 2025 and `_inquiry_only` leaves the
    other two columns NULL, so every deal has at most one stamp anywhere near a
    bound, and "every stamp is older than its bound" agrees with "the *newest*
    stamp is older than its bound" everywhere the file looks. Review R1 found
    two mutants that survived all 736 tests through exactly that gap: taking
    `max(pairs)` and bounding only that one, and handing `last_guest_reply_at`
    the inquiry bound.

    So the deals below each carry two non-NULL stamps inside the 24h band
    between the bounds, in both directions, because the two spellings fail
    opposite ways:

    * `inquiry_at` in the band with a *newer* `last_contact_at` already past the
      server bound. The aggregate closes this deal; the per-column rule must
      not, because its `inquiry_at` has not reached its own property bound and
      "early in the property frame" is the whole thing `min()` prevents. It is
      also the ordinary shape of the accepted cost — a listing dated 22 days
      ago, contacted the same afternoon, never replied to — which is why the
      `inquiry_stale_before` comment now names this population and not only the
      never-contacted one.
    * `last_guest_reply_at` in the band with a newer `last_contact_at`. That
      column really is a server stamp and keeps the server bound, so the close
      must still happen; widening it to the inquiry bound would veto here.

    Asserted at the predicate *and* through `advance_lifecycle`, so it also
    holds the two bounds to the columns the call site pairs them with.
    """
    today, server_today = "2026-03-09", "2026-03-10"       # property a day behind
    inquiry_bound = (datetime.fromisoformat(today) - timedelta(days=STALE)
                     ).isoformat(timespec="seconds")
    server_bound = (datetime.fromisoformat(server_today) - timedelta(days=STALE)
                    ).isoformat(timespec="seconds")
    band = (datetime.fromisoformat(inquiry_bound) + timedelta(hours=12)
            ).isoformat(timespec="seconds")
    newer = (datetime.fromisoformat(band) + timedelta(hours=6)
             ).isoformat(timespec="seconds")
    # The band has to be a real band or every assertion below is vacuous: both
    # stamps must be stale by the SERVER bound and fresh by the INQUIRY one.
    assert inquiry_bound < band < newer < server_bound, (
        inquiry_bound, band, newer, server_bound)

    # --- the predicate, directly ---
    older_inquiry = {"inquiry_at": band, "last_contact_at": newer,
                     "next_action_at": None}
    assert pipeline._is_abandoned(older_inquiry, server_bound,
                                  inquiry_bound) is False, \
        "an inquiry_at short of its own bound must veto even when it is not the newest"
    replied_then_contacted = {"last_guest_reply_at": band,
                              "last_contact_at": newer,
                              "inquiry_at": INQUIRY_FAR_BEHIND,
                              "next_action_at": None}
    # Vacuity guard on the fixture: if the guest were the last to speak, the
    # early return above the bounds would answer and the pairing would go
    # untested.
    assert pipeline._guest_is_waiting(replied_then_contacted) is False, \
        "vacuity guard: this deal must reach the bounds loop"
    assert pipeline._is_abandoned(replied_then_contacted, server_bound,
                                  inquiry_bound) is True, \
        "last_guest_reply_at is a server stamp and must keep the server bound"
    # And the aggregate this replaced really does disagree on the first deal, so
    # the assertions above are not two spellings of the same answer.
    assert max(pipeline.cmp_ts(band), pipeline.cmp_ts(newer)) < \
        pipeline.cmp_ts(server_bound), "the old max() spelling closed this deal"

    # --- the same two shapes through the write path ---
    _open_deal(tenant, "inq-in-band", "A", newer)
    _open_deal(tenant, "replied-then-contacted", "B", newer)
    _open_deal(tenant, "both-past", "C", newer)      # positive control
    with pipeline._conn() as c:
        c.execute("UPDATE deals SET inquiry_at=? WHERE tenant_id=? AND site=? "
                  "AND item_id=?", (band, str(tenant), SITE, "inq-in-band"))
        c.execute("UPDATE deals SET last_guest_reply_at=? WHERE tenant_id=? "
                  "AND site=? AND item_id=?",
                  (band, str(tenant), SITE, "replied-then-contacted"))

    moved = pipeline.advance_lifecycle(tenant, SITE, today=today,
                                       server_today=server_today)
    stages = _stages(tenant)

    assert stages["inq-in-band"] == pipeline.CONTACTED, stages
    assert stages["replied-then-contacted"] == pipeline.LOST, stages
    assert stages["both-past"] == pipeline.LOST, ("positive control", stages)
    assert moved["lost"] == 2, (moved, stages)


def test_a_deal_with_no_stamp_at_all_is_never_abandoned(tenant):
    """VEN-225 AC5 — `_is_abandoned` at the unit level, including its default.

    Preserved behaviour rather than new: the old spelling's `bool(last)` guard
    said the same thing, and the restatement has to keep saying it. The write
    path cannot produce a row with all three columns NULL, but a legacy row can,
    and "we know nothing about this deal" must never read as "this deal is
    dead" — that close is irreversible without a human.

    The third argument's **default** is asserted here too, because the
    two-argument call is public API as far as any other caller is concerned: it
    must fall back to the server bound rather than to no bound at all.
    """
    bound = "2026-02-17T00:00:00"
    old, new = "2026-01-01T00:00:00", "2026-03-01T00:00:00"

    assert pipeline._is_abandoned({}, bound, bound) is False
    assert pipeline._is_abandoned({"inquiry_at": None, "last_contact_at": None,
                                   "last_guest_reply_at": None}, bound, bound) is False
    # Positive controls on the same shape, or the assertions above would also
    # hold of a function that had stopped returning True at all.
    assert pipeline._is_abandoned({"inquiry_at": old}, bound, bound) is True
    assert pipeline._is_abandoned({"last_contact_at": old}, bound, bound) is True
    assert pipeline._is_abandoned({"inquiry_at": new}, bound, bound) is False

    # The default: with no inquiry bound supplied, `inquiry_at` is held to the
    # server bound, which is exactly what every pre-VEN-225 caller expected.
    assert pipeline._is_abandoned({"inquiry_at": old}, bound) is True
    assert pipeline._is_abandoned({"inquiry_at": new}, bound) is False


def test_equal_bounds_make_the_per_column_rule_the_old_max_rule(tenant):
    """VEN-225 AC6 — the no-property-zone case is a *literal* no-op.

    `advance_lifecycle` derives both bounds from dates that are equal whenever
    no property timezone is set, and the docstring claims that "every stamp is
    older than its own bound" is then the same sentence as "the newest stamp is
    older than the bound". That is an equivalence claim over a function's whole
    input space, so it is asserted as one, against the pre-VEN-225 spelling
    written out below — and enumerated rather than sampled, because the case
    that breaks such a restatement is never the obvious one.

    The shipped `_is_abandoned` is imported; only the reference copy is local.
    The reverse — comparing two local copies — would assert nothing about what
    actually runs.
    """
    bound = "2026-02-17T00:00:00"

    def old_max_spelling(deal):
        last = max(pipeline.cmp_ts(deal.get("last_guest_reply_at")),
                   pipeline.cmp_ts(deal.get("last_contact_at")),
                   pipeline.cmp_ts(deal.get("inquiry_at")))
        return bool(last) and last < pipeline.cmp_ts(bound)

    # Either side of the bound, exactly on it, and NULL, for all three columns.
    values = (None, "2026-01-01T00:00:00", "2026-02-16T23:59:59", bound,
              "2026-02-17T00:00:01", "2026-03-01T00:00:00")
    checked = 0
    for reply in values:
        for contact in values:
            for inquiry in values:
                deal = {"last_guest_reply_at": reply, "last_contact_at": contact,
                        "inquiry_at": inquiry}
                # `_guest_is_waiting` short-circuits both spellings identically,
                # and comparing them there would assert nothing about this change.
                if pipeline._guest_is_waiting(deal):
                    continue
                assert pipeline._is_abandoned(deal, bound, bound) is \
                    old_max_spelling(deal), deal
                checked += 1
    assert checked > 100, ("the grid collapsed to almost nothing", checked)
    # And the grid really does contain both answers, so agreement is not
    # agreement on a constant.
    assert old_max_spelling({"inquiry_at": "2026-01-01T00:00:00"})
    assert not old_max_spelling({"inquiry_at": "2026-03-01T00:00:00"})


def test_untouched_deals_are_unchanged_when_no_property_zone_is_set(
        tenant, monkeypatch):
    """VEN-225 AC6, behavioural half — the same claim through the write path.

    The equivalence above is about `_is_abandoned` in isolation; this is about
    the two bounds `advance_lifecycle` derives, on the population VEN-223's
    no-zone test never had (it seeds `_open_deal`s, whose `inquiry_at` is pinned
    out of the way). Two identically seeded tenants, one on the default code
    path and one on the explicit dates the pre-VEN-225 code computed.
    """
    other = "2"
    config.save_settings(other, host_name="Other Host", timezone="")
    assert config.get_settings(tenant).get("timezone") in ("", None)

    server_now = datetime.combine(REF_DAY, time(23, 30))
    server_date = server_now.date()
    for tid in (tenant, other):
        _untouched(tid, "edge", server_date - timedelta(days=STALE))
        _untouched(tid, "cold", server_date - timedelta(days=STALE + 1))
        _untouched(tid, "warm", server_date - timedelta(days=5))

    _freeze(monkeypatch, server_now, pipeline, scheduler)
    # With no property zone `local_now` falls back to the server frame, so the
    # two dates the fix derives are the one date the old code used.
    assert scheduler.local_now(tenant).date() == server_date

    new_default = pipeline.advance_lifecycle(tenant, SITE)
    old_behaviour = pipeline.advance_lifecycle(
        other, SITE, today=server_date.isoformat(),
        server_today=server_date.isoformat())

    assert new_default == old_behaviour, (new_default, old_behaviour)
    assert _stages(tenant) == _stages(other), (_stages(tenant), _stages(other))
    # Non-vacuous: the run closed the cold deal and left the other two.
    assert new_default["lost"] == 1, new_default
    assert _stages(tenant) == {"edge": pipeline.NEW, "cold": pipeline.LOST,
                               "warm": pipeline.NEW}, _stages(tenant)


def test_a_deal_in_the_ahead_direction_closes_a_day_late_on_purpose(
        tenant, monkeypatch):
    """VEN-225 AC7 — the accepted cost on legacy-framed rows, re-scoped by VEN-226.

    After VEN-226 the new writer stores `inquiry_at` in the server frame, so a
    genuinely 22-property-day-old deal now closes on time. The one-day delay that
    VEN-225 accepted as a cost only applies to rows whose `inquiry_at` is still in
    the legacy property frame — written before VEN-226, or whose item no longer
    parses — where the stored prefix cannot be attributed to either frame.

    This test asserts that cost on exactly that population: three deals SQL-stamped
    in the legacy format (`f"{date}T09:00:00"`) so the abandonment bound still
    protects them. The VEN-225 rationale stands for these rows; removing `min()` is
    not this ticket's call.

    `colder` and `warm` are controls: one that definitely closes (verifies the arm
    fires at all) and one that stays open (verifies it doesn't over-fire).
    """
    zone, server_now, prop_date = _zone_where_property_is(AHEAD)
    assert prop_date == server_now.date() + timedelta(days=1), (zone, prop_date)
    config.save_settings(tenant, timezone=zone)

    # SQL-stamp directly in the legacy property frame so the abandonment bound
    # protects these rows exactly as VEN-225 described.
    for item_id, listing_date in (
        ("cold", prop_date - timedelta(days=STALE + 1)),
        ("colder", prop_date - timedelta(days=STALE + 2)),
        ("warm", prop_date - timedelta(days=5)),
    ):
        _inquiry_only(tenant, item_id, f"{listing_date.isoformat()}T09:00:00")

    _freeze(monkeypatch, server_now, pipeline, scheduler)
    moved = pipeline.advance_lifecycle(tenant, SITE)

    ctx = (zone, server_now.date(), prop_date, moved)
    assert _stage(tenant, "colder") == pipeline.LOST, ("positive control", ctx)
    assert _stage(tenant, "warm") == pipeline.NEW, ("negative control", ctx)
    assert _stage(tenant, "cold") == pipeline.NEW, ("one day late, on purpose", ctx)
    assert moved["lost"] == 1, ctx


# --- the no-op case ---------------------------------------------------------


def test_tenant_without_a_property_timezone_is_unchanged(tenant, monkeypatch):
    """AC5 — byte-identical to the old server-frame behaviour when no zone is set.

    Asserted against the **default code path** rather than a literal date, by
    running two identically seeded tenants side by side: one through the new
    default, one through the server date the old code would have computed. A
    literal-date assertion would only restate the arithmetic.
    """
    import db

    other = "2"
    config.save_settings(other, host_name="Other Host", timezone="")
    assert config.get_settings(tenant).get("timezone") in ("", None)

    server_now = datetime.combine(REF_DAY, time(23, 30))
    server_date = server_now.date()
    for tid in (tenant, other):
        _booked(tid, "arriving", "A", check_in=server_date.isoformat())
        _booked(tid, "soon", "B",
                check_in=(server_date + timedelta(days=3)).isoformat())
        _booked(tid, "done", "C", check_in="2026-02-01",
                check_out=(server_date - timedelta(days=1)).isoformat())
        _open_deal(tid, "cold", "D",
                   (datetime.combine(server_date, time(0, 0))
                    - timedelta(days=pipeline.STALE_CLOSE_DAYS, hours=12)
                    ).isoformat(timespec="seconds"))

    _freeze(monkeypatch, server_now, pipeline, scheduler)

    # With no property zone `local_now` falls back to the server frame, so the
    # date the fix resolves *is* the one the old code used.
    assert scheduler.local_now(tenant).date() == server_date
    assert db.DB_PATH  # both tenants share this run's database file

    new_default = pipeline.advance_lifecycle(tenant, SITE)
    old_behaviour = pipeline.advance_lifecycle(
        other, SITE, today=server_date.isoformat(),
        server_today=server_date.isoformat())

    assert new_default == old_behaviour, (new_default, old_behaviour)
    assert _stages(tenant) == _stages(other)
    # And the run was not a no-op, or the comparison above would be empty.
    assert new_default == {"pre_arrival": 1, "staying": 1, "completed": 1, "lost": 1}


# --- import safety ----------------------------------------------------------


def test_importing_pipeline_starts_no_thread_and_forms_no_cycle():
    """AC6 — the fix adds `import scheduler` to `pipeline`.

    Run in a subprocess because thread count is process-global and pytest has
    already imported half the app by the time this file loads. VEN-162 shipped
    an import that leaked a scheduler thread, which is why this is measured
    rather than reasoned about.
    """
    script = textwrap.dedent("""
        import threading, sys
        before = threading.active_count()
        import pipeline
        print(before, threading.active_count(),
              'scheduler' in sys.modules,
              sorted(t.name for t in threading.enumerate()))
    """)
    env = dict(os.environ, PYTHONPATH=REPO)
    out = subprocess.run([sys.executable, "-c", script], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr  # an import cycle would raise here
    before, after, imported, names = out.stdout.split(maxsplit=3)
    assert imported == "True", out.stdout
    assert before == after == "1", (out.stdout, "importing pipeline leaked a thread")
    assert "MainThread" in names, out.stdout


# --- structural guard: a clean merge must not strand a name -----------------


def _unbound_loads(source: str, func_name: str) -> list[str]:
    """Names `func_name` reads without binding, ignoring module-level names.

    Factored out so the checker itself can be given a fired control below — an
    absence assertion whose matcher has silently stopped matching passes
    forever, which is the failure mode this shape is most prone to.
    """
    import builtins

    tree = ast.parse(source)
    bound = set(dir(builtins))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                bound.update(n.id for n in ast.walk(t) if isinstance(n, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)

    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == func_name)
    args = fn.args
    bound.update(a.arg for a in args.args + args.kwonlyargs + args.posonlyargs)
    for extra in (args.vararg, args.kwarg):
        if extra:
            bound.add(extra.arg)
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bound.update(a.arg for a in node.args.args + node.args.kwonlyargs
                             + node.args.posonlyargs)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    return sorted({n.id for n in ast.walk(fn)
                   if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)} - bound)


def test_unbound_name_checker_actually_fires():
    """The positive control for the guard below.

    Without this, a checker that had stopped finding anything would keep the
    guard green forever — the exact way an absence assertion rots.
    """
    stranded = textwrap.dedent("""
        import db
        def f(a):
            b = a + 1
            return b + local_now
    """)
    assert _unbound_loads(stranded, "f") == ["local_now"]
    assert _unbound_loads(stranded.replace("local_now", "db"), "f") == []


def test_advance_lifecycle_reads_no_name_it_does_not_bind():
    """AC7 — asserts the *class* of defect a clean merge can produce here.

    Two open PRs edit `pipeline.py` (#55 and #49). Neither touches this
    function's body today, but VEN-221 measured the shape that makes this worth
    guarding: a merge whose hunks are more than three lines apart applies
    cleanly and silently, and can leave a function reading a name the other
    side deleted. `advance_lifecycle` is a worker write path, so a `NameError`
    there is a lifecycle that stops advancing for every tenant, with nothing in
    the request path to surface it.
    """
    unbound = _unbound_loads(Path(REPO, "pipeline.py").read_text(),
                             "advance_lifecycle")
    assert not unbound, (
        f"advance_lifecycle reads {unbound} without binding it — that raises "
        "NameError for every tenant on the next worker pass")
