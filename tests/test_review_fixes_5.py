"""Regression tests for D1, the defect the fifth re-review left open on `da44a3e`.

D1 is an *interaction* defect: neither commit that produced it is wrong alone.

- Ungating the reclaim (`dashboard.py`) made a **second host** read `sending_at`
  for the first time on the worker-queue topology. Before that, the web dyno
  never touched the column — the worker read only its own stamps, where writer
  timezone and reader timezone are the same by construction.
- Stamping the claim absolute (`outbox._now_utc`) fixes every row written after
  deploy, but says nothing about rows already sitting in `sending` with a naive
  stamp when the new code starts reading them.

For that version-skew window the old code read a naive stamp with
`stamp.astimezone()` — i.e. as the *reader's* local zone. A writer west of the
reader therefore produced a stamp that resolved to an instant hours in the past,
so a one-second-old live claim looked stale and was requeued while the first
drainer was still driving the browser. The guest receives the message twice, and
because `queued` *is* in `CANCELABLE` the operator then gets a green "cancelled"
toast for a message physically going out.

The `stamp > now` guard caught only the **eastward** case (future-dated). West
fell straight through. That asymmetry is the whole bug, which is why the test
below is a *matrix over writer offset* rather than a single stale-stamp case: a
one-directional guard passes any single-direction test.

Every test here was verified to fail against `da44a3e` — and to fail for the
filed reason (a live send requeued at negative writer offsets), not merely to
fail.
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


@pytest.fixture(autouse=True)
def drainer_spy(monkeypatch):
    """Replace the drainer with a counter, for every test in this file.

    `autouse` on purpose: a test added here later that forgets the fixture
    would start a real browser thread, and the docstring below is a promise
    about the whole file, not about whoever remembers to ask.

    Two jobs. No test may start a real browser thread: `automation._draining`
    is a module global that stays set for up to 30 s and silently suppresses
    every later `start_drainer` in the process, which makes any unspied
    measurement on the `_board` gate depend on test order. And every test gets
    to *assert* whether the drainer was reached instead of assuming it.

    Patching `automation.start_drainer` — the attribute `dashboard` looks up at
    call time — replaces the whole function, so this is also independent of any
    gate placed *inside* the drainer.
    """
    calls = []
    monkeypatch.setattr(automation, "start_drainer", lambda site: calls.append(site))
    return calls


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review5.db")
    monkeypatch.setattr(pipeline, "_TS_NORMALIZED", False, raising=False)
    tid = "1"
    config.save_settings(tid, host_name="Test Host", timezone="America/New_York")
    return tid


def _deal(tenant_id, item_id, *, kind="lead", guest="Dana R."):
    item = {"id": item_id, "kind": kind, "traveler": guest,
            "title": f"Unit | {guest}", "property_name": ""}
    storage.filter_new(tenant_id, SITE, kind, [item])
    pipeline.ensure(tenant_id, SITE, item, None)
    return item


def _queued(tenant_id, item_id="s1", body="hello"):
    _deal(tenant_id, item_id)
    return outbox.add(tenant_id, SITE, item_id, sequence="presale",
                      step_id="intro", step_label="First reply",
                      body=body, auto=True)


def _claim_naive_at(msg_id, writer_offset_hours):
    """Stamp `sending_at` the *pre-upgrade* way, as a host at the given offset.

    A naive local wall-clock stamp for a claim made one second ago, by a host
    whose UTC offset is `writer_offset_hours`. This is exactly what any row
    already in flight at deploy time carries.
    """
    wall = (datetime.now(timezone.utc)
            + timedelta(hours=writer_offset_hours)
            - timedelta(seconds=1))
    stamp = wall.replace(tzinfo=None).isoformat(timespec="seconds")
    with outbox._conn() as c:
        c.execute("UPDATE outbox SET sending_at=? WHERE id=?", (stamp, msg_id))
    return stamp


# The reader is this process. The writer is any host sharing the DATABASE_URL,
# so the offset between them spans the real range — Hawaii to New Zealand,
# including the half-hour zones that a naive hour-based guard would miss.
WRITER_OFFSETS = [-10, -8, -4, -0.5, 0, 3.5, 5.5, 9, 13]


@pytest.mark.parametrize("writer_offset", WRITER_OFFSETS)
def test_a_live_send_is_never_requeued_whatever_the_writers_offset(
        tenant, monkeypatch, drainer_spy, writer_offset):
    """D1: a one-second-old claim must survive a dashboard render.

    Driven through `_board`, not through `reclaim_stuck_sending` directly. That
    distinction has now cost two review rounds: the helper compares naive stamps
    identically on both heads, so calling it directly makes the regression
    invisible. It is reachable from a second host *only* because the reclaim was
    ungated, and that is the call site the guest's duplicate message comes from.
    """
    import dashboard

    # The row is already `sending`, so `outbox.queued_tenants()` is empty and no
    # drainer is due on this render — asserted below rather than assumed. (The
    # claim belongs to some other process that is, right now, driving a browser.)
    #
    # The gate is pinned *open* deliberately. It is not under test here, and
    # inverting this patch does not change the result — by design: what proves
    # the absence is the empty queue, and pinning the first conjunct true is
    # what stops that proof from evaporating on a host without Playwright,
    # where the ambient gate is false and would mask the second conjunct
    # entirely. The gate itself is covered two-sided by
    # `test_the_capability_gate_decides_whether_a_render_starts_a_drainer`.
    monkeypatch.setattr(dashboard, "_can_deliver_in_process", lambda: True)
    msg = _queued(tenant)
    outbox.set_status(msg["id"], outbox.SENDING)
    _claim_naive_at(msg["id"], writer_offset)

    with dashboard.app.test_request_context():
        dashboard._board(tenant)

    assert outbox.get(msg["id"])["status"] == outbox.SENDING, (
        f"a 1s-old live send was requeued for a writer at UTC{writer_offset:+}; "
        "it is being delivered a second time while the first drainer is still "
        "driving the browser")
    assert drainer_spy == [], (
        "the sending row must not put a drainer on this render")


@pytest.mark.parametrize("writer_offset", WRITER_OFFSETS)
def test_the_naive_stamp_is_replaced_rather_than_reinterpreted(
        tenant, monkeypatch, drainer_spy, writer_offset):
    """The row must not merely survive — it must stop being ambiguous.

    A naive stamp cannot be attributed to a host, so no reading of it is sound.
    Leaving it in place would make the row permanently unreclaimable, trading a
    duplicate send for a stranded one. The pass that declines to judge it must
    therefore restamp it absolute, so the *next* pass has a stamp it can trust.

    As above, the row is already `sending`, so no drainer is due on this render,
    and the gate is pinned open so the empty queue is what proves it.
    """
    import dashboard

    monkeypatch.setattr(dashboard, "_can_deliver_in_process", lambda: True)
    msg = _queued(tenant)
    outbox.set_status(msg["id"], outbox.SENDING)
    _claim_naive_at(msg["id"], writer_offset)

    with dashboard.app.test_request_context():
        dashboard._board(tenant)

    after = datetime.fromisoformat(outbox.get(msg["id"])["sending_at"])
    assert after.tzinfo is not None, (
        "the ambiguous naive stamp survived the pass, so the row can never be "
        "judged and a genuinely crashed send is stranded forever")
    assert drainer_spy == [], (
        "the sending row must not put a drainer on this render")


def test_a_genuinely_stranded_send_still_recovers_on_the_next_pass(
        tenant, monkeypatch, drainer_spy):
    """The cost of declining to judge a naive stamp, pinned so it stays bounded.

    A pre-upgrade wedged row needs two passes: the first restamps it absolute,
    the second measures a real age against it. That is the deliberate trade —
    a stranded row recovers up to max_age_seconds (900 s) later, where the
    alternative was delivering a live message twice. This test exists so the
    second pass cannot quietly stop working.

    Unlike its two neighbours, the `_can_deliver_in_process` patch below is
    load-bearing: the second pass leaves the row `queued`, so `queued_tenants()`
    is non-empty and the gate is the conjunct that decides. Inverting the patch
    changes `drainer_spy`. Do not remove it.
    """
    import dashboard

    monkeypatch.setattr(dashboard, "_can_deliver_in_process", lambda: False)
    msg = _queued(tenant)
    outbox.set_status(msg["id"], outbox.SENDING)
    _claim_naive_at(msg["id"], 0)

    before = datetime.now(timezone.utc).replace(microsecond=0)
    with dashboard.app.test_request_context():
        dashboard._board(tenant)
    assert outbox.get(msg["id"])["status"] == outbox.SENDING
    written = datetime.fromisoformat(outbox.get(msg["id"])["sending_at"])
    assert written >= before, (
        "pass 1 must restamp absolute and current, not in the past — "
        "a past stamp reintroduces the double-delivery defect"
    )

    # Second pass, with the (now absolute) stamp aged past the reclaim window.
    old = (datetime.now(timezone.utc) - timedelta(seconds=3600))
    with outbox._conn() as c:
        c.execute("UPDATE outbox SET sending_at=? WHERE id=?",
                  (old.isoformat(timespec="seconds"), msg["id"]))
    with dashboard.app.test_request_context():
        dashboard._board(tenant)

    assert outbox.get(msg["id"])["status"] == outbox.QUEUED, (
        "a genuinely crashed send must still be reclaimed on the pass after "
        "its stamp was made unambiguous")
    assert drainer_spy == [], (
        "the reclaim left the row queued, so the capability gate is the only "
        "thing keeping a drainer off a host that cannot deliver in-process")


@pytest.mark.parametrize("capable,expected", [(False, 0), (True, 1)])
def test_the_capability_gate_decides_whether_a_render_starts_a_drainer(
        tenant, monkeypatch, drainer_spy, capable, expected):
    """Two-sided positive control on the `_board` drainer gate.

    The rest of this file drives rows that are already `sending`, so
    `outbox.queued_tenants()` is empty and the gate never gets to decide — a
    `_can_deliver_in_process` patch there is inert in *both* directions (VEN-161).
    Here the row is left genuinely `queued`, so the gate is the deciding
    conjunct and inverting the patch has to change the result.

    Both halves matter. `capable=False` catches the gate being deleted (an
    incapable host spinning up a drainer that cannot finish); `capable=True`
    catches it being welded shut, which is the direction nothing else in the
    suite covers — queued messages would silently stop being delivered from a
    render on every topology, and a test that only asserts an absence passes
    that regression happily.
    """
    import dashboard

    monkeypatch.setattr(dashboard, "_can_deliver_in_process", lambda: capable)
    msg = _queued(tenant)

    assert outbox.get(msg["id"])["status"] == outbox.QUEUED
    assert outbox.queued_tenants() == [tenant], (
        "precondition: the second conjunct must be truthy, or this test is "
        "inert in the exact way VEN-161 filed")

    with dashboard.app.test_request_context():
        dashboard._board(tenant)

    # `[SITE] * expected` and not `[SITE] if capable else []`: the expectation
    # has to be driven by the parametrized column, or inverting it changes
    # nothing and this test acquires the very defect VEN-161 filed.
    assert drainer_spy == [SITE] * expected, (
        f"_can_deliver_in_process()={capable} must yield {expected} drainer "
        f"start(s) for {SITE} from a render with a queued row, got {drainer_spy}")
