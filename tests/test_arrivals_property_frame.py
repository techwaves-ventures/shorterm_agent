"""VEN-221: the arrivals window is bounded by the *property's* calendar date.

`pipeline.arrivals` bounded `check_in` with `datetime.now().date()` — the
**server's** date. `check_in` is not a machine stamp: it has exactly two write
paths (`pipeline.parse_date` off the FurnishedFinder listing, and the operator
form behind `mark_booked`) and neither reads a clock. It is the zoneless
calendar date the guest stated and the listing shows. Comparing it against the
server's date is therefore a comparison spanning two frames, and for the several
hours a day the two zones disagree it is off by a day at both ends:

  * a guest **arriving today at the property** silently vanishes from the
    arrivals list on the very day they arrive (a UTC dyno past 17:00 US-Pacific
    is already on tomorrow's date, so `today <= check_in` is false);
  * symmetrically, the far edge of the horizon pulls in or drops a day early.

This is the sibling of VEN-138 with the opposite remedy, and that is not a
contradiction: #53 moved the digest's 24h window *into* the server frame because
`created_at`/`sent_at` are `pipeline._now()` server stamps, and this moves the
arrivals bound *into* the property frame because `check_in` is a property date.
One rule — compare each column in the frame it was written in — so `digest.build`
correctly ends up holding both frames at once.

`metrics()` is threaded as well as `arrivals()`. Fixing only `arrivals()` leaves
the `arrivals_30d` KPI tile counting the server frame while the Arrivals list
directly beneath it counts the property frame — two surfaces disagreeing is
worse than the shared bug, so `test_kpi_tile_counts_exactly_the_arrivals_listed`
exists specifically to fail on that half-fix.

Why these tests never touch `TZ`/`tzset()`: `datetime.now()` is process-global,
so pinning the server clock poisons every other test in the run (it broke
`test_review_fixes_5.py` about one run in four while VEN-138 was being built).
Because this fix makes the date *injectable*, most of these tests need no clock
at all — they pass the date in. The two that exercise the real wiring instead
pick a property zone **relative to the runner's own instant** and assert the two
dates actually differ before asserting on behaviour, so a run where the choice
came out vacuous goes red rather than quietly green.

Standalone (no pytest required to run it directly):

    PYTHONPATH=. ./.venv/bin/python -m pytest tests/test_arrivals_property_frame.py
"""
import ast
import builtins
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
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
import pipeline  # noqa: E402
import scheduler  # noqa: E402
import storage  # noqa: E402

SITE = "furnishedfinder"

# Spans UTC-11 to UTC+14 — a 25h spread, so at *any* instant these zones are
# never all on the same calendar date. That is what guarantees the wiring tests
# below can always find a property zone whose date differs from the runner's,
# whatever zone the runner sits in.
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


def _booked(tenant_id: str, item_id: str, guest: str, check_in: str) -> None:
    """A deal in a BOOKED stage whose `check_in` is exactly `check_in`.

    Written through `mark_booked`, the real operator path, rather than by
    UPDATE — the point of the ticket is what that column means, so a test that
    invented its own writer could be pinning a value the product never stores.
    """
    item = {"id": item_id, "kind": "lead", "traveler": guest,
            "title": f"Unit 1 | Washington, District of Columbia | {guest}",
            "received_at": (datetime.now() - timedelta(days=2)).strftime("%B %-d, %Y")}
    storage.filter_new(tenant_id, SITE, "lead", [item])
    pipeline.ensure(tenant_id, SITE, item, None)
    pipeline.mark_booked(tenant_id, SITE, item_id, check_in=check_in)


def _shift(day: str, days: int) -> str:
    """`day` (a bare YYYY-MM-DD) moved by `days`, in the same bare shape.

    Spelled out with `date` arithmetic rather than string slicing so the month
    and year boundaries are real.
    """
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def _check_ins(deals: list[dict]) -> list[str]:
    return [d["check_in"] for d in deals]


def _zone_whose_date_differs(ref: datetime) -> tuple[str, str, str]:
    """A candidate zone on a different calendar date than the server, at `ref`.

    Returns `(zone_name, property_date, server_date)`. Nothing is mutated: the
    zone is chosen to suit the instant, rather than the instant being forced to
    suit a hard-coded zone. Prefers a zone *behind* the server, because that is
    the direction of the filed harm (the guest arriving today drops out); falls
    back to one ahead, which is reachable only when the runner is already at the
    far western end of the list.
    """
    server_date = ref.astimezone().date().isoformat()
    behind, ahead = [], []
    for name in CANDIDATE_ZONES:
        prop_date = ref.astimezone(ZoneInfo(name)).date().isoformat()
        if prop_date < server_date:
            behind.append((name, prop_date))
        elif prop_date > server_date:
            ahead.append((name, prop_date))
    pick = (behind or ahead)
    # Vacuity guard: with a 25h spread this cannot legitimately be empty, so an
    # empty list means the candidate list or the selection logic rotted — fail
    # rather than skip, which would hide the loss of coverage.
    assert pick, f"no candidate zone differs in date from the server at {ref!r}"
    name, prop_date = pick[0]
    assert prop_date != server_date
    return name, prop_date, server_date


def _arrivals_block(body: str) -> list[str]:
    """The check-in dates the digest listed under "Arriving in the next N days".

    Parsed out of the real rendered body rather than reaching into internals:
    the body is what the owner actually receives. Scoped to the block, because
    the same guest also appears in the "new inquiries" block with a `from
    <check_in>` suffix, so a bare substring search over the whole body would
    pass no matter what the arrivals filter did.
    """
    dates: list[str] = []
    grabbing = False
    for line in body.split("\n"):
        if grabbing:
            m = re.match(r"^  - .* on (\d{4}-\d{2}-\d{2})$", line)
            if m:
                dates.append(m.group(1))
                continue
            break
        if re.match(r"^Arriving in the next \d+ days: \d+$", line):
            grabbing = True
    return dates


# --- the defect -------------------------------------------------------------


def test_guest_arriving_on_the_property_date_is_listed_not_dropped():
    """The filed harm, and its control on the same deal set.

    Both halves matter. The *absent* assertion alone would pass against a fix
    that never fires — it is satisfied by any filter that happens to exclude the
    guest. The *present* assertion is what proves the property date actually
    reaches the comparison.
    """
    prop_today = "2026-03-09"          # the property's calendar date
    server_today = "2026-03-10"        # the server has already rolled over
    deals = [{"stage": pipeline.BOOKED, "check_in": prop_today, "guest_name": "Arriving Today"}]

    assert _check_ins(pipeline.arrivals(deals, within_days=7, today=server_today)) == [], (
        "control failed: the server date is supposed to drop this guest — if it "
        "does not, the rest of this test proves nothing"
    )
    assert _check_ins(pipeline.arrivals(deals, within_days=7, today=prop_today)) == [prop_today]


def test_horizon_is_derived_from_the_given_day_not_a_second_clock_reading():
    """The far edge moves with `today`, and is inclusive at exactly `+within_days`.

    On `main` `horizon` came from a *second, independent* `datetime.now()` call,
    so it stayed pinned to the server's day even once `today` was injectable —
    a half-fix in which the near edge honours the property and the far edge does
    not. Asserting the boundary from two different `today` values is what
    distinguishes "derived from `today`" from "read off the clock again".
    """
    prop_today = "2026-03-09"
    deals = [
        {"stage": pipeline.BOOKED, "check_in": _shift(prop_today, -1), "guest_name": "Yesterday"},
        {"stage": pipeline.BOOKED, "check_in": prop_today, "guest_name": "Today"},
        {"stage": pipeline.BOOKED, "check_in": _shift(prop_today, 7), "guest_name": "Edge"},
        {"stage": pipeline.BOOKED, "check_in": _shift(prop_today, 8), "guest_name": "Past edge"},
    ]

    got = _check_ins(pipeline.arrivals(deals, within_days=7, today=prop_today))
    assert got == [prop_today, _shift(prop_today, 7)], (
        "both bounds are inclusive of the property's own day and exclusive "
        "outside the window"
    )

    # Shift only `today`; every bound must move with it by exactly one day.
    tomorrow = _shift(prop_today, 1)
    got = _check_ins(pipeline.arrivals(deals, within_days=7, today=tomorrow))
    assert got == [_shift(prop_today, 7), _shift(prop_today, 8)]


def test_horizon_crosses_a_month_boundary_correctly():
    """Guards the arithmetic itself, not just the frame.

    `horizon` is built by parsing `today` and adding days; a naive string
    implementation looks right in mid-month and breaks at the rollover.
    """
    deals = [{"stage": pipeline.BOOKED, "check_in": "2026-03-03", "guest_name": "March"}]
    assert _check_ins(pipeline.arrivals(deals, within_days=7, today="2026-02-27")) == ["2026-03-03"]
    assert _check_ins(pipeline.arrivals(deals, within_days=2, today="2026-02-27")) == []


def test_kpi_tile_counts_exactly_the_arrivals_listed():
    """`metrics()` must forward `today` — the half-fix this test exists to catch.

    `arrivals_30d` is rendered as a KPI tile immediately above the Arrivals list
    on the dashboard. Threading `arrivals()` and leaving `metrics()` alone makes
    the tile and the list disagree on the same screen.
    """
    prop_today = "2026-03-09"
    server_today = "2026-03-10"
    deals = [
        {"stage": pipeline.BOOKED, "check_in": prop_today, "guest_name": "Arriving Today"},
        {"stage": pipeline.BOOKED, "check_in": _shift(prop_today, 5), "guest_name": "Later"},
    ]

    assert pipeline.metrics(deals, {}, today=server_today)["arrivals_30d"] == 1, (
        "control: the server date drops the arriving-today guest from the tile too"
    )
    tile = pipeline.metrics(deals, {}, today=prop_today)["arrivals_30d"]
    assert tile == len(pipeline.arrivals(deals, today=prop_today)) == 2


# --- the wiring: does the property date actually reach the comparison? -------


def test_digest_lists_the_guest_arriving_on_the_property_calendar_today(tenant):
    """AC4 end-to-end: assert on the rendered email, not on the intermediate list.

    The deliverable is an email, so the check that matters is whether the guest's
    check-in date appears in the "Arriving in the next 7 days" block of the body
    the owner receives.

    `now` is passed in as an **aware** instant, so the property-local date is
    fixed by the zone alone and this test does not depend on the runner's zone.
    """
    zone = "America/Los_Angeles"
    config.save_settings(tenant, timezone=zone)

    # 02:30 UTC on the 10th is 19:30 on the 9th in Los Angeles: the server has
    # rolled to a new date and the property has not.
    now = datetime(2026, 3, 10, 2, 30, tzinfo=ZoneInfo("UTC"))
    prop_today = scheduler.local_now(tenant, now).date().isoformat()
    server_today = now.astimezone().date().isoformat()
    assert prop_today == "2026-03-09"
    assert prop_today != server_today, "vacuity: the two frames must differ here"

    _booked(tenant, "a1", "Arriving Today", prop_today)
    _booked(tenant, "a2", "Arriving Later", _shift(prop_today, 3))

    built = digest.build(tenant, now=now)
    assert built is not None
    listed = _arrivals_block(built["body"])
    assert prop_today in listed, (
        f"the guest arriving today at the property is missing from the digest; "
        f"block listed {listed}"
    )
    assert listed == [prop_today, _shift(prop_today, 3)]


def test_board_arrivals_and_kpi_tile_both_use_the_property_date(tenant):
    """AC5: the dashboard's list and its tile, on one instant, in one frame.

    `_board` reads the clock itself, so rather than freezing it this picks a
    property zone that is *already* on a different date than the runner — always
    possible given the 25h spread of the candidate list, and asserted before any
    behaviour is asserted.
    """
    import dashboard

    zone, prop_today, server_today = _zone_whose_date_differs(datetime.now().astimezone())
    config.save_settings(tenant, timezone=zone)
    assert prop_today != server_today

    # One guest on each date, so whichever frame the board uses is legible from
    # the result — and neither assertion can pass by accident.
    _booked(tenant, "b1", "Property Today", prop_today)
    _booked(tenant, "b2", "Server Today", server_today)

    board = dashboard._board(tenant)
    listed = sorted(c["deal"]["check_in"] for c in board["arrivals"])
    expected = sorted(_check_ins(pipeline.arrivals(
        pipeline.all_deals(tenant, SITE), today=prop_today)))

    assert listed == expected, (
        f"board arrivals came from the wrong day: {listed} != {expected} "
        f"(property {prop_today}, server {server_today})"
    )
    assert prop_today in listed, (
        "positive control: a guest arriving on the property's own date must be "
        "on the board"
    )
    assert board["metrics"]["arrivals_30d"] == len(board["arrivals"]), (
        "the KPI tile and the Arrivals list beneath it disagree — metrics() is "
        "not being threaded the same day the list is"
    )


def test_tenant_without_a_property_timezone_is_unchanged(tenant):
    """AC7: no timezone set means no behaviour change at all.

    `tz_for` returns None, `local_now` falls back to the server frame, and the
    derived date is the same one the old no-argument call produced. Asserted
    against the *default* code path rather than a literal, so this stays true if
    the default ever changes.
    """
    import dashboard

    assert not str(config.get_settings(tenant).get("timezone") or "").strip()
    server_today = datetime.now().date().isoformat()
    assert scheduler.local_now(tenant).date().isoformat() == server_today

    _booked(tenant, "c1", "Today", server_today)
    _booked(tenant, "c2", "Soon", _shift(server_today, 4))

    deals = pipeline.all_deals(tenant, SITE)
    board = dashboard._board(tenant)
    assert sorted(c["deal"]["check_in"] for c in board["arrivals"]) == sorted(
        _check_ins(pipeline.arrivals(deals))), (
        "an unconfigured tenant must see exactly what the pre-fix no-argument "
        "call returned"
    )
    assert board["metrics"]["arrivals_30d"] == pipeline.metrics(deals, {})["arrivals_30d"]


# --- structural guards ------------------------------------------------------


def _module_ast(name: str) -> ast.Module:
    return ast.parse(Path(REPO, f"{name}.py").read_text(), filename=f"{name}.py")


def test_every_in_repo_call_site_passes_today():
    """AC6: the `today=None` default is fail-open, so the call sites are pinned.

    Keeping the default preserves source compatibility and matches
    `advance_lifecycle`'s existing signature — but it means a caller who forgets
    the argument silently gets the bug back, with a green suite. This asserts
    mechanically that every in-repo call passes it, so a *new* unpassed call
    site fails here rather than shipping the defect a third time.
    """
    found = 0
    for module in ("digest", "dashboard"):
        for node in ast.walk(_module_ast(module)):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Name) and fn.value.id == "pipeline"
                    and fn.attr in ("arrivals", "metrics")):
                continue
            found += 1
            kwargs = {k.arg for k in node.keywords}
            assert "today" in kwargs, (
                f"{module}.py:{node.lineno} calls pipeline.{fn.attr}() without an "
                f"explicit `today=` — it will silently use the server's date"
            )
    # A matcher that quietly stops matching would make the loop above vacuous.
    assert found == 4, f"expected 4 call sites across digest.py/dashboard.py, matched {found}"


def test_digest_build_reads_no_name_it_does_not_bind():
    """AC8: guards the measured clean-merge regression that strands a variable.

    Writing this fix the obvious way on `main` reuses the `local_now` binding
    that sits ~9 lines above the edit. PR #53 deletes that binding. The two
    hunks are further apart than git's 3 lines of context, so the merge is
    **clean, zero conflicts** — and the merged `digest.build` reads a name
    nothing assigns, raising `NameError: local_now` for every tenant, killing
    the daily digest outright.

    Both PRs are green in isolation and only the merge product fails, so no
    behavioural test on either branch can catch it. This one can: a name read in
    `build` must be a local, a parameter, a module global, or a builtin.
    """
    build = next(n for n in _module_ast("digest").body
                 if isinstance(n, ast.FunctionDef) and n.name == "build")

    bound = set(dir(builtins)) | set(vars(digest))
    for node in ast.walk(build):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
            bound.update(a.arg for a in node.args.args + node.args.kwonlyargs)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in node.names)

    unbound = sorted({n.id for n in ast.walk(build)
                      if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                      and n.id not in bound})
    assert not unbound, (
        f"digest.build reads name(s) nothing binds: {unbound}. If this is "
        f"`local_now`, a merge stranded it — re-derive under a distinct name "
        f"instead of reusing a binding another change may delete (VEN-221 §2)."
    )
