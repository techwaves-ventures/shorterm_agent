"""VEN-138: the daily digest must cover the last 24 hours of *real* time.

`digest.build()` took its window boundary from `scheduler.local_now()` — the
**property's** wall clock — and then string-compared it against `created_at`
and `sent_at`, which `pipeline._now()` and `outbox._now()` write in the
**server's** naive wall clock. Two frames in one comparison, so the window slid
by (property offset - server offset):

  * a property *east* of the server silently lost the tail of its own day (a
    Tokyo property on a UTC dyno drops everything from 18:00 to 03:00 — the
    digest under-reports work the owner paid for, with no visible signal);
  * a property *west* of the server reported yesterday's activity as today's,
    because the filter is `>= since` with no upper bound.

Neither needs the split-host topology VEN-134 depends on: one process is enough
as soon as the tenant sets a Property timezone that differs from the server's.

Why these tests never touch `TZ`/`tzset()`: `datetime.now()` is process-global,
so pinning the server clock poisons every other test in the run (it broke
`test_review_fixes_5.py` about one run in four while this was being built).
Instead each test measures the *runner's own* offset and picks a property zone
relative to it, mutating nothing. That also makes the tests server-zone
agnostic: the candidate list below spans UTC-11..UTC+14, so whatever offset the
runner sits at, at least one candidate is >= 6h away from it (asserted, so a
vacuous run fails rather than passing quietly).

Standalone (no pytest required to run it directly):

    PYTHONPATH=. ./.venv/bin/python -m pytest tests/test_digest_window_frame.py
"""
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
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
import digest  # noqa: E402
import outbox  # noqa: E402
import pipeline  # noqa: E402
import storage  # noqa: E402

SITE = "furnishedfinder"

# Spans UTC-11 to UTC+14, so max(offset) - min(offset) is 25h. Whatever the
# runner's own offset, one end of this list is therefore at least 12.5h away
# from it — the guarantee that makes these tests exercise the defect from any
# server timezone, including the extremes.
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


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    """A clean tenant on its own database file, with no property timezone set.

    Starting with no timezone means the fixture itself is in the server frame;
    each test opts into a property zone explicitly, so it is always visible
    which frame a given assertion is about.
    """
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    tid = "1"
    config.save_settings(tid, host_name="Test Host", timezone="",
                         digest_enabled="1", digest_hour="18:00",
                         autopilot="0", check_times="09:00,16:00",
                         last_check_at="", last_digest_at="")
    return tid


# --- helpers ----------------------------------------------------------------


def _delta_hours(zone_name: str, ref: datetime) -> float:
    """(property offset - server offset) at instant `ref`, in hours.

    Computed straight from `zoneinfo`, deliberately not from
    `scheduler.local_now` — a fixture that asks the code under test how far to
    shift would move with the bug instead of pinning it.
    """
    aware = ref.astimezone()  # `ref` as an absolute instant, in the server's zone
    prop = aware.astimezone(ZoneInfo(zone_name))
    return (prop.utcoffset() - aware.utcoffset()).total_seconds() / 3600.0


def _extreme_zones(ref: datetime) -> list[tuple[str, float]]:
    """The candidate zones furthest east and furthest west of the server."""
    deltas = {z: _delta_hours(z, ref) for z in CANDIDATE_ZONES}
    east = max(deltas, key=lambda z: deltas[z])
    west = min(deltas, key=lambda z: deltas[z])
    return [(east, deltas[east]), (west, deltas[west])]


def _stamp(ref: datetime, hours_ago: float) -> str:
    """A stamp in the shape `pipeline._now`/`outbox._now` write: naive, server
    wall clock, `T`-separated, seconds precision."""
    return (ref - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _lead(tenant_id: str, item_id: str, guest: str, created_at: str) -> None:
    """A deal whose `created_at` is exactly `created_at`.

    The column is written by `pipeline._now()` on insert, so the age is set
    afterwards by writing the column directly — same value the real writer
    would have produced at that moment, no clock moved.
    """
    item = {"id": item_id, "kind": "lead", "traveler": guest,
            "title": f"Unit 1 | Washington, District of Columbia | {guest}",
            "received_at": (datetime.now() - timedelta(days=2)).strftime("%B %-d, %Y")}
    storage.filter_new(tenant_id, SITE, "lead", [item])
    pipeline.ensure(tenant_id, SITE, item, None)
    with pipeline._conn() as c:
        c.execute(
            "UPDATE deals SET created_at=? WHERE tenant_id=? AND site=? AND item_id=?",
            (created_at, str(tenant_id), SITE, item_id),
        )


def _sent_message(tenant_id: str, item_id: str, label: str, sent_at: str) -> None:
    """A `sent` outbox row whose `sent_at` is exactly `sent_at`."""
    msg = outbox.add(tenant_id, SITE, item_id, sequence="presale", step_id="intro",
                     step_label=label, body="Hi", auto=True)
    outbox.set_status(msg["id"], outbox.SENT)
    with outbox._conn() as c:
        c.execute("UPDATE outbox SET sent_at=? WHERE id=?", (sent_at, msg["id"]))


def _new_inquiries(built: dict | None) -> list[str]:
    """The guest names the digest listed under "N new inquiries".

    Parsed out of the real rendered body rather than reaching into internals:
    the body is what the owner actually receives, and the same guest also
    appears in the "still waiting on you" block, so a bare substring check on
    the whole body would pass no matter what the window did.
    """
    if not built:
        return []
    names: list[str] = []
    grabbing = False
    for line in built["body"].split("\n"):
        if grabbing:
            if line.startswith("  - "):
                names.append(line[4:].split(",")[0].strip())
                continue
            break
        if re.match(r"^\d+ new inquir(y|ies):$", line):
            grabbing = True
    return names


def _sent_labels(built: dict | None) -> list[str]:
    """The step labels listed under "The agent sent N replies today"."""
    if not built:
        return []
    labels: list[str] = []
    grabbing = False
    for line in built["body"].split("\n"):
        if grabbing:
            m = re.match(r"^  - .*\(([^)]*)\)$", line)
            if m:
                labels.append(m.group(1))
                continue
            break
        if re.match(r"^The agent sent \d+ repl(y|ies) today:$", line):
            grabbing = True
    return labels


# --- the defect -------------------------------------------------------------


def test_new_inquiry_window_is_the_last_24h_whatever_the_property_timezone(tenant):
    """A deal is "new" iff it was created in the last 24 hours of real time.

    Run against the property zone furthest east of the runner and the one
    furthest west. For an eastward property the base code's boundary sits
    `delta` hours *late*, so it drops genuinely-new leads; for a westward one it
    sits `delta` hours *early*, so it reports stale ones. Each direction gets
    the probe that its own failure mode produces, plus an inside and an outside
    control that both heads agree on — so a uniformly failing run is
    distinguishable from a discriminating one.
    """
    now = datetime.now().replace(microsecond=0)
    zones = _extreme_zones(now)
    assert zones[0][1] - zones[1][1] >= 12, (
        "candidate zones must straddle the server by at least 12h", zones)

    exercised: list[float] = []
    problems: list[str] = []
    for i, (zone, delta) in enumerate(zones):
        if abs(delta) < 1:
            continue  # the other extreme carries this run; the assert below proves one did
        exercised.append(abs(delta))
        config.save_settings(tenant, timezone=zone)

        if delta > 0:
            # Eastward: base's window ends `delta` hours late, so it starts
            # `delta` hours late too and excludes anything older than
            # (24 - delta). This lead is younger than 24h — genuinely new.
            probe_age, probe_expected = 24 - delta / 2, True
        else:
            # Westward: base's window starts `|delta|` hours early, so it reaches
            # back past 24h. This lead is older than 24h — not new any more.
            probe_age, probe_expected = 24 + abs(delta) / 2, False

        _lead(tenant, f"P{i}", f"Probe{i}", _stamp(now, probe_age))
        _lead(tenant, f"I{i}", f"Inside{i}", _stamp(now, 0.5))
        _lead(tenant, f"O{i}", f"Outside{i}", _stamp(now, 24 + abs(delta) + 6))

        built = digest.build(tenant)  # now=None: the path automation.py/worker.py use
        assert built is not None, f"{zone}: digest built nothing to assert on"
        names = _new_inquiries(built)
        where = f"{zone} (delta {delta:+.1f}h)"

        # Collected rather than asserted in place: both directions fail on the
        # unfixed code, and stopping at the first one hides the second.
        if f"Inside{i}" not in names:
            problems.append(f"{where}: 30min-old lead missing")
        if f"Outside{i}" in names:
            problems.append(f"{where}: lead older than the widest window reported")
        if probe_expected and f"Probe{i}" not in names:
            problems.append(
                f"{where}: a lead {probe_age:.1f}h old is inside the last 24h but the "
                "digest dropped it — the window moved with the property zone")
        if not probe_expected and f"Probe{i}" in names:
            problems.append(
                f"{where}: a lead {probe_age:.1f}h old is outside the last 24h but the "
                "digest reported it — the window moved with the property zone")

    assert exercised and max(exercised) >= 6, (
        "no candidate zone was far enough from the server to move the window; "
        "this test would have proven nothing", zones)
    assert not problems, "\n".join(problems)


def test_sent_today_window_is_the_last_24h_whatever_the_property_timezone(tenant):
    """Same boundary, the other column it filters: `outbox.sent_at`.

    Worth its own test because `created_at` and `sent_at` are written by
    different modules (`pipeline._now` vs `outbox._now`) and read through
    different code paths in `build()`; a fix applied to one comparison and not
    the other would still pass the test above.
    """
    now = datetime.now().replace(microsecond=0)
    zones = _extreme_zones(now)

    exercised: list[float] = []
    problems: list[str] = []
    for i, (zone, delta) in enumerate(zones):
        if abs(delta) < 1:
            continue
        exercised.append(abs(delta))
        config.save_settings(tenant, timezone=zone)

        if delta > 0:
            probe_age, probe_expected = 24 - delta / 2, True
        else:
            probe_age, probe_expected = 24 + abs(delta) / 2, False

        _lead(tenant, f"M{i}", f"Guest{i}", _stamp(now, 100))
        _sent_message(tenant, f"M{i}", f"PROBE{i}", _stamp(now, probe_age))
        _sent_message(tenant, f"M{i}", f"INSIDE{i}", _stamp(now, 0.5))
        _sent_message(tenant, f"M{i}", f"OUTSIDE{i}", _stamp(now, 24 + abs(delta) + 6))

        built = digest.build(tenant)
        assert built is not None, f"{zone}: digest built nothing to assert on"
        labels = _sent_labels(built)
        where = f"{zone} (delta {delta:+.1f}h)"

        if f"INSIDE{i}" not in labels:
            problems.append(f"{where}: 30min-old send missing")
        if f"OUTSIDE{i}" in labels:
            problems.append(f"{where}: send older than the widest window reported")
        if probe_expected and f"PROBE{i}" not in labels:
            problems.append(
                f"{where}: a send {probe_age:.1f}h old is inside the last 24h but the "
                "digest dropped it")
        if not probe_expected and f"PROBE{i}" in labels:
            problems.append(
                f"{where}: a send {probe_age:.1f}h old is outside the last 24h but the "
                "digest reported it")

    assert exercised and max(exercised) >= 6, ("vacuous run", zones)
    assert not problems, "\n".join(problems)


# --- controls: these pass on the unfixed code and must keep passing ---------


def test_window_is_exactly_24_hours_wide(tenant):
    """With no property timezone there is no frame mismatch, so this control
    passes on the unfixed code — its job is to pin the window's *size*.

    Without it, a boundary of 23h or 25h satisfies every assertion above (the
    probes above sit half an offset away from the boundary, which is far too
    coarse to notice an hour).
    """
    now = datetime.now().replace(microsecond=0)
    _lead(tenant, "J", "JustInside", _stamp(now, 23.5))
    _lead(tenant, "K", "JustOutside", _stamp(now, 24.5))

    names = _new_inquiries(digest.build(tenant))
    assert "JustInside" in names, "a 23.5h-old lead is inside a 24h window"
    assert "JustOutside" not in names, "a 24.5h-old lead is outside a 24h window"


def test_same_frame_tenant_is_unaffected(tenant):
    """The no-property-timezone case must be untouched by the fix — this is the
    control that proves the tests above discriminate rather than failing
    uniformly, since it is green on both heads."""
    now = datetime.now().replace(microsecond=0)
    _lead(tenant, "R", "Recent", _stamp(now, 1))
    _lead(tenant, "S", "Stale", _stamp(now, 30))

    names = _new_inquiries(digest.build(tenant))
    assert "Recent" in names
    assert "Stale" not in names


def test_aware_now_is_converted_to_the_server_frame_not_stripped(tenant):
    """`build(now)` accepts an aware datetime, and must convert it.

    Two ways to get this wrong, both of which walked through the whole suite
    while the fix was being written:

      * `.replace(tzinfo=None)` without `.astimezone()` — drops the offset
        instead of converting, so the boundary lands in whatever frame the
        caller happened to use (the exact `scheduler.py` bug, one layer down);
      * leaving the value aware — `isoformat()` then appends `+00:00`, and these
        columns are compared as *strings*, so the suffix breaks the ordering.

    The aware zone is built relative to the runner's own offset so the two are
    guaranteed to differ whatever the server's zone is.
    """
    now = datetime.now().replace(microsecond=0)
    server_offset = now.astimezone().utcoffset()
    caller_tz = timezone(server_offset + timedelta(hours=7))
    aware_now = now.astimezone().astimezone(caller_tz)  # same instant, +7h wall clock

    _lead(tenant, "A", "InWindow", _stamp(now, 20))
    _lead(tenant, "B", "OutOfWindow", _stamp(now, 28))
    _lead(tenant, "C", "OnBoundary", _stamp(now, 24))

    names = _new_inquiries(digest.build(tenant, aware_now))
    assert "InWindow" in names, (
        "a 20h-old lead fell outside the window when `now` arrived aware — the "
        "offset was stripped rather than converted")
    assert "OutOfWindow" not in names
    # Only the equality case can see a boundary that is correct in value but
    # still aware: "…T09:00:00" >= "…T09:00:00+00:00" is False, because the bare
    # stamp is a strict prefix of the suffixed one.
    assert "OnBoundary" in names, (
        "the lead created exactly 24h ago was excluded — the boundary kept a tz "
        "suffix, and these columns are compared as strings")


def test_boundary_stamp_is_naive_and_the_boundary_instant_is_included(tenant):
    """A deal created exactly on the boundary is inside the window (`>=`).

    This is what catches a boundary that is correct in value but still carries a
    tz suffix: `"2026-01-01T00:00:00" >= "2026-01-01T00:00:00+00:00"` is False,
    because the bare stamp is a strict prefix of the suffixed one. Equality is
    the only place that mutant shows up, so it needs its own case.
    """
    now = datetime.now().replace(microsecond=0)
    _lead(tenant, "E", "ExactlyOnBoundary", _stamp(now, 24))
    _sent_message(tenant, "E", "ONBOUNDARY", _stamp(now, 24))

    built = digest.build(tenant, now)
    assert "ExactlyOnBoundary" in _new_inquiries(built), (
        "the lead created exactly 24h ago was excluded — the boundary is not a "
        "bare naive stamp in the same shape as the columns it filters")
    # Both comparisons, not just the first: `created_at` and `sent_at` are read
    # through separate expressions, so one can be inclusive while the other is not.
    assert "ONBOUNDARY" in _sent_labels(built), (
        "the message sent exactly 24h ago was excluded from the digest's send count")
