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
`last_contact_at` / `inquiry_at`, which `pipeline._now()` writes in **server**
wall clock. Re-pointing the single `today` at the property fixes the three stage
arms and breaks the abandonment arm by exactly the same offset — a deal that is
genuinely three weeks cold stops closing. So `today` had to be **split** into
two values, not moved, and one function now legitimately holds two frames. Same
rule as VEN-138, applied in the opposite direction: compare each column in the
frame it was written in.

`test_property_frame_today_does_not_move_the_abandonment_bound` and
`test_worker_call_path_keeps_the_abandonment_bound_in_the_server_frame` are the
two halves of that guard, and they are the tests that go red if a later change
collapses the two dates back together.

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

    `inquiry_at` is pushed behind it on purpose: `_is_abandoned` takes the
    *max* of three columns, so leaving the default inquiry stamp newer would
    make the abandonment assertions answer about the wrong column.
    """
    _deal(tenant_id, item_id, guest)
    pipeline.update(tenant_id, SITE, item_id, stage=pipeline.CONTACTED,
                    last_contact_at=last_contact, next_action_at=None)
    with pipeline._conn() as c:
        c.execute(
            "UPDATE deals SET inquiry_at=? WHERE tenant_id=? AND site=? AND item_id=?",
            ("2025-01-05T09:00:00", str(tenant_id), SITE, item_id))


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
    the misanchored bound sweeps 13h45m further back and takes `warm` with it.

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
