"""VEN-133: a host whose *clock* runs ahead strands the send it claims.

`sending_at` carries its UTC offset (`outbox._now_utc`), which settles the
*timezone* half of one host reading another host's stamp. It says nothing about
that host's *clock*. A dyno running fast writes an absolute instant that has not
happened yet, and `reclaim_stuck_sending` judged age with a single test —
`stamp >= cutoff` — under which a future stamp is never stale. The row is not
mis-aged, it is immune: it stays `sending` until real time overtakes the skew.

That is a stranded send, and there is no other way out of `sending`. `SENDING`
is not in `outbox.CANCELABLE`, so the card reads "Sending…" and Cancel refuses;
`has_open_step` counts the row open, so the agent never re-drafts that step for
that guest. A day of skew is a day of a guest not being answered.

Measured through `dashboard._board` — the real call site, at the real default
`max_age_seconds` — with the reclaiming host on a correct clock and rendering
every 15 minutes:

    writer clock skew | recovery on 0f0100e | recovery with the guard
                   +0 |              15 min |                  15 min
               +15min |              30 min |                  30 min
                   +1h |             75 min |                  30 min
                  +24h |           1455 min |                  30 min

Two things this file deliberately does *not* claim.

* It does not cover a clock running **behind**. That writes a stamp which is
  merely old, and an old stamp is exactly what a genuinely crashed send leaves
  behind — from the row alone the two are indistinguishable, so there is no
  predicate to write. Measured on `0f0100e`: a host 15 minutes slow has its
  one-second-old live send requeued on the first render, delivering the message
  twice. Closing that needs send *ownership* (is the claiming process alive),
  not a better reading of the clock — see VEN-145's `claim_token`. The
  asymmetry here is a property of the information, not an oversight, which is
  why it is not the same mistake the naive branch made (there, both directions
  were detectable and only one was guarded).
* It says nothing about `reclaim_stuck_sending`'s lack of a `tenant_id` filter.
  Scoping it by tenant is measured *unsafe* (VEN-165: the claim path is global
  too, so scoping the reclaim strands the row permanently on the topologies
  where `dashboard.py` is the only reclaimer).

Measured against `0f0100e` (the base this was written on): **9 of the 15 cases
fail there, and fail for the filed reason** — a future-dated row still `sending`
after the render ceiling, and a future stamp still sitting in the column after
the pass. The other six are green on both heads *by design*, and it is worth
being exact about which, because "green on base" is otherwise indistinguishable
from "blind":

* four are controls that must not change — the zero-skew row of the ceiling
  test, `..._live_send_is_left_alone...`, `..._genuinely_stale_send_is_still_
  reclaimed...` and `..._stamped_at_this_very_instant_is_live_not_future`. Their
  job is to fail if the fix over-reaches, so a guard that requeued live sends,
  restamped stale ones, or swallowed the boundary would be caught by them and by
  nothing else here;
* two pin properties of the *new branch itself* — that its write is scoped by
  `id`, and that it carries `AND status=?`. There is no such branch on the base,
  so nothing there can fail them.

All six are validated by mutation instead of by the base: a 13-mutant battery
over this branch (comparison direction and strictness, a day of tolerance, the
`continue`, both WHERE terms, the clock the replacement is written on, and the
branch's position relative to the age test) is **13/13 killed by this file
alone** — no other test file counted.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("FF_CRED_KEY", "c9jwUi0L-fUjf3wjbq74M0lK3ah7fmEfGhjxZ7RehQk=")
os.environ.setdefault("SECRET_KEY", "test-secret")

import automation  # noqa: E402
import config  # noqa: E402
import outbox  # noqa: E402
import pipeline  # noqa: E402
import storage  # noqa: E402

SITE = "furnishedfinder"
RENDER_EVERY = timedelta(minutes=15)
_REAL_DATETIME = datetime


@pytest.fixture(autouse=True)
def drainer_spy(monkeypatch):
    """No test here may start a real browser thread.

    `autouse` for the reason the same fixture is autouse in
    `test_review_fixes_5.py`: `automation._draining` is a module global that
    suppresses every later `start_drainer` in the process, so one unspied test
    makes its neighbours order-dependent.
    """
    calls = []
    monkeypatch.setattr(automation, "start_drainer", lambda site: calls.append(site))
    return calls


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "skew.db")
    monkeypatch.setattr(pipeline, "_TS_NORMALIZED", False, raising=False)
    tid = "1"
    config.save_settings(tid, host_name="Test Host", timezone="America/New_York")
    return tid


@pytest.fixture()
def clock(monkeypatch):
    """Move the *clock*, never the stored data.

    Both hosts in this file are the same process, distinguished only by what
    their clock reads. Swapping a `datetime` subclass onto `outbox.datetime`
    puts the offset behind `_now_utc()` — so the stamp under test is written by
    the function that really writes it, through `set_status(SENDING)`, rather
    than hand-poked into the column. A hand-written stamp would keep passing if
    the claim path stopped using `_now_utc` altogether.

    Returns a setter taking a `timedelta` to add to real time.
    """
    state = {"delta": timedelta(0)}

    class _Clock(_REAL_DATETIME):
        @classmethod
        def now(cls, tz=None):
            return _REAL_DATETIME.now(tz) + state["delta"]

    monkeypatch.setattr(outbox, "datetime", _Clock)

    def _set(delta):
        state["delta"] = delta

    return _set


@pytest.fixture()
def frozen_clock(monkeypatch):
    """A stopped clock, on a whole second, for the `stamp == now` boundary.

    `_now_utc` truncates to seconds while `datetime.now(timezone.utc)` carries
    microseconds, so under a running clock a stamp can essentially never come
    out exactly equal to the instant the reclaim reads — the boundary between
    "claimed now" and "claimed in the future" is unreachable, and an off-by-one
    on that comparison would be untestable rather than absent. Stopping the
    clock makes both sides read the same instant by construction.
    """
    frozen = _REAL_DATETIME.now(timezone.utc).replace(microsecond=0)

    class _Frozen(_REAL_DATETIME):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz else frozen.replace(tzinfo=None)

    monkeypatch.setattr(outbox, "datetime", _Frozen)
    return frozen


def _deal(tenant_id, item_id, *, guest="Dana R."):
    item = {"id": item_id, "kind": "lead", "traveler": guest,
            "title": f"Unit | {guest}", "property_name": ""}
    storage.filter_new(tenant_id, SITE, "lead", [item])
    pipeline.ensure(tenant_id, SITE, item, None)
    return item


def _queued(tenant_id, item_id="s1", body="hello"):
    _deal(tenant_id, item_id)
    return outbox.add(tenant_id, SITE, item_id, sequence="presale",
                      step_id="intro", step_label="First reply",
                      body=body, auto=True)


def _claim_on_a_clock(msg_id, skew, clock):
    """Claim the send as a host whose clock is `skew` off real time."""
    clock(skew)
    outbox.set_status(msg_id, outbox.SENDING)
    clock(timedelta(0))
    return outbox.get(msg_id)["sending_at"]


@pytest.fixture()
def render(clock, monkeypatch):
    """One `dashboard._board` render at `at` after real now, on a correct clock.

    `_can_deliver_in_process` is pinned *closed* for the whole file. Nothing
    here is about the drainer gate — it is covered two-sided in
    `test_review_fixes_5.py` — and pinning it stops the result depending on
    whether the host running the suite happens to have Playwright.
    """
    import dashboard

    monkeypatch.setattr(dashboard, "_can_deliver_in_process", lambda: False)

    def _render(tenant_id, at):
        clock(at)
        with dashboard.app.test_request_context():
            dashboard._board(tenant_id)

    return _render


# A skew is a wrong clock, not a timezone, so the realistic range is not the
# zone range: a dyno with a broken NTP client drifts by minutes, a suspended VM
# resumes hours or days out. `(0, 1)` is the control — a correctly-clocked host
# must still recover in a single render, or this guard has cost a pass it had no
# right to cost.
SKEWS = [
    (timedelta(0), 1),
    (timedelta(minutes=20), 2),
    (timedelta(hours=1), 2),
    (timedelta(hours=24), 2),
    (timedelta(days=7), 2),
]


@pytest.mark.parametrize("skew,max_renders", SKEWS)
def test_recovery_is_bounded_however_far_ahead_the_claiming_clock_ran(
        tenant, clock, render, drainer_spy, skew, max_renders):
    """The headline: time-to-recovery must not scale with the skew.

    Driven through `_board` at the default `max_age_seconds`, not through
    `reclaim_stuck_sending(max_age_seconds=-1)`. That distinction is the whole
    test: at a negative age every row is stale on sight, so the age comparison
    this ticket is about never runs, which is why the shipped reclaim assertion
    in `test_agent_lifecycle.py` stayed green through the defect.

    `max_renders` is asserted as an exact ceiling rather than "recovers
    eventually": on `0f0100e` these all recover *eventually* — that is the
    defect, a day later.
    """
    msg = _queued(tenant)
    _claim_on_a_clock(msg["id"], skew, clock)

    renders = 0
    while outbox.get(msg["id"])["status"] == outbox.SENDING:
        renders += 1
        assert renders <= max_renders, (
            f"a send claimed by a clock {skew} fast was still 'Sending…' after "
            f"{renders - 1} renders ({(renders - 1) * RENDER_EVERY}); the "
            "operator cannot cancel it and the agent will not re-draft the step")
        render(tenant, renders * RENDER_EVERY)

    assert outbox.get(msg["id"])["status"] == outbox.QUEUED
    assert drainer_spy == [], (
        "these renders are on a host that cannot deliver in-process")


@pytest.mark.parametrize("skew", [s for s, _ in SKEWS if s])
def test_a_future_stamp_is_replaced_rather_than_left_to_expire(
        tenant, clock, render, drainer_spy, skew):
    """The mechanism behind the test above: the pass that declines to judge a
    future stamp must leave behind one it *can* judge.

    Waiting the skew out would also "recover", eventually — that is exactly the
    defect. So this asserts the stored stamp moved back to the reclaiming host's
    own clock, which is the only thing that makes the next pass's age real.
    """
    msg = _queued(tenant)
    written = _claim_on_a_clock(msg["id"], skew, clock)
    before = datetime.now(timezone.utc).replace(microsecond=0)

    render(tenant, timedelta(seconds=1))

    after = datetime.fromisoformat(outbox.get(msg["id"])["sending_at"])
    assert after.tzinfo is not None, "the replacement must stay absolute"
    assert after != datetime.fromisoformat(written), (
        f"the future stamp {written} survived the pass, so the row can only "
        "become stale once real time overtakes the skew")
    assert before <= after <= datetime.now(timezone.utc) + timedelta(seconds=2), (
        "the replacement must be the reclaiming host's now — a stamp in the "
        "past would requeue a live send on the very next pass")
    assert outbox.get(msg["id"])["status"] == outbox.SENDING, (
        "replacing the stamp is not the same as reclaiming the row; this pass "
        "still knows nothing about the send's real age")


def test_a_live_send_is_left_alone_on_the_pass_that_fixes_its_stamp(
        tenant, clock, render, drainer_spy):
    """Control, and the inverse of the guard: restamping must not requeue.

    The skewed host's claim may be perfectly live — a browser is driving it
    right now. Turning "I cannot read this stamp" into "requeue it" would hand
    that send to a second drainer and deliver the guest's message twice, which
    is the harm `reclaim_stuck_sending` exists to avoid, not to cause.
    """
    msg = _queued(tenant)
    _claim_on_a_clock(msg["id"], timedelta(hours=6), clock)

    for n in (1, 2, 3):
        render(tenant, timedelta(seconds=n))
        assert outbox.get(msg["id"])["status"] == outbox.SENDING, (
            f"a send claimed {n}s ago was requeued into a second drainer while "
            "the first is still driving the browser")
    assert outbox.get(msg["id"])["attempts"] == 1, (
        "no second claim was made, so the attempt count must not have moved")


def test_a_claim_stamped_at_this_very_instant_is_live_not_future(
        tenant, frozen_clock):
    """The boundary: the guard is `stamp > now`, strictly.

    A claim stamped at the exact instant the reclaim reads is the most recent
    live claim there can be, not a skewed one — it must go on to be judged by
    age like any other row rather than be diverted as unreadable. Relaxing the
    comparison to `>=` diverts it, and that misclassification is invisible under
    a running clock (see `frozen_clock`) and under the 900 s default (a
    future-dated stamp satisfies the age test anyway). It takes a stopped clock
    *and* a sweep that treats everything as stale to make the two readings
    disagree, which is precisely why it is pinned here rather than left to be
    noticed later.
    """
    msg = _queued(tenant)
    outbox.set_status(msg["id"], outbox.SENDING)
    stamp = datetime.fromisoformat(outbox.get(msg["id"])["sending_at"])
    assert stamp == frozen_clock, (
        "precondition: the stored stamp must be the very instant the reclaim "
        "reads, or this test is inert in both directions")

    assert outbox.reclaim_stuck_sending(max_age_seconds=-1) == 1, (
        "a stamp equal to now is not ahead of now; it must reach the age test")
    assert outbox.get(msg["id"])["status"] == outbox.QUEUED


def test_the_pass_that_replaces_a_stamp_never_also_acts_on_it(tenant, clock):
    """The branch ends in `continue`, and this is the only thing that pins it.

    At the 900 s default the `continue` is unobservable — a future stamp also
    satisfies the age test below it, so falling through changes nothing. It
    becomes load-bearing exactly when a caller asks for an aggressive sweep: at
    a negative `max_age_seconds` the cutoff moves *ahead* of now, and a stamp
    an hour in the future is then "old enough", so a pass that had just declared
    that stamp unreadable would turn round and requeue the send on the strength
    of it. The invariant is the one both sibling branches keep: a stamp we have
    replaced is not evidence about anything, this pass included.

    Called directly rather than through `_board`, because `_board` only ever
    passes the default and this is a statement about the other callers.
    """
    msg = _queued(tenant)
    _claim_on_a_clock(msg["id"], timedelta(hours=1), clock)

    assert outbox.reclaim_stuck_sending(max_age_seconds=-7200) == 0
    assert outbox.get(msg["id"])["status"] == outbox.SENDING, (
        "the send was requeued on the strength of the very stamp this pass had "
        "just decided it could not read")


def test_a_genuinely_stale_send_is_still_reclaimed_on_the_first_pass(
        tenant, clock, render, drainer_spy):
    """Control: the guard must not swallow the ordinary route.

    A stamp in the *past*, beyond `max_age_seconds`, is the case this function
    was written for. It must still be reclaimed in one pass — a guard that
    restamped these instead would keep resetting their age and strand every
    crashed send in the system, which is a strictly worse version of the bug
    being fixed here.
    """
    msg = _queued(tenant)
    _claim_on_a_clock(msg["id"], timedelta(0), clock)

    render(tenant, timedelta(seconds=901))

    assert outbox.get(msg["id"])["status"] == outbox.QUEUED, (
        "a send whose start is 901s in the past is what 'stuck' means")


def test_replacing_one_rows_stamp_does_not_touch_another(
        tenant, clock, render, drainer_spy):
    """The restamp is scoped to the row being judged, by id.

    Two rows for two guests, both `sending`: one claimed by the fast clock, one
    genuinely wedged. Un-scoping the write would re-date the wedged row's start
    from a decision taken about its neighbour, resetting the age of a send that
    has been stuck for an hour every time any other row needs fixing.
    """
    fast = _queued(tenant, item_id="s1")
    wedged = _queued(tenant, item_id="s2", body="second guest")
    _claim_on_a_clock(fast["id"], timedelta(hours=3), clock)
    _claim_on_a_clock(wedged["id"], timedelta(0), clock)
    with outbox._conn() as c:
        c.execute("UPDATE outbox SET sending_at=? WHERE id=?",
                  ("2026-01-01T00:00:00+00:00", wedged["id"]))

    render(tenant, timedelta(seconds=1))

    assert outbox.get(wedged["id"])["sending_at"] == "2026-01-01T00:00:00+00:00", (
        "the wedged row's start was rewritten by the pass over its neighbour")
    assert outbox.get(wedged["id"])["status"] == outbox.QUEUED
    assert outbox.get(fast["id"])["status"] == outbox.SENDING


def test_a_send_that_completes_mid_pass_does_not_get_its_stamp_rewritten(
        tenant, clock, drainer_spy):
    """The restamp carries the same `AND status=?` as every other write here.

    `reclaim_stuck_sending` writes from a snapshot read earlier in the pass, so
    a send that reaches a terminal state in that window is written by a decision
    taken about a row that no longer exists in that state. The sibling case is
    covered for the requeue write in `test_review_fixes_3.py`; this is the same
    invariant on the branch this ticket adds. Drop `AND status=?` from it and
    this fails.
    """
    msg = _queued(tenant)
    written = _claim_on_a_clock(msg["id"], timedelta(hours=3), clock)

    # The send finishes between the SELECT and the UPDATE. Interleave on the
    # *same* connection — a second one would just block on the open write txn.
    real_conn = outbox._conn
    held = real_conn()

    class _Shared:
        """The live connection, minus the close-on-exit."""
        def __getattr__(self, name):
            return getattr(held, name)

        def __enter__(self):
            return held

        def __exit__(self, *exc):
            return False

    real_row = outbox._row

    def _row_then_complete(row):
        out = real_row(row)
        if out and out.get("status") == outbox.SENDING:
            held.execute("UPDATE outbox SET status=?, sent_at=? WHERE id=?",
                         (outbox.SENT, "2026-01-01T00:00:00", out["id"]))
        return out

    # Restored by hand rather than via monkeypatch.undo(), which would also
    # revert the `tenant` fixture's DB_PATH and point `get` at another database.
    outbox._conn, outbox._row = _Shared, _row_then_complete
    try:
        outbox.reclaim_stuck_sending()
    finally:
        outbox._conn, outbox._row = real_conn, real_row
        held.raw.commit()
        held.raw.close()

    row = outbox.get(msg["id"])
    assert row["status"] == outbox.SENT
    assert row["sending_at"] == written, (
        "a delivered send's start time was rewritten from a stale snapshot")
