"""VEN-149: a process that cannot drive a browser must not win the send claim.

The defect is a *topology* defect, so every test here runs with the gate
**closed** — `FORCE_WORKER_QUEUE=1`, the real production lever for the
documented Vercel-web + `worker.py` deployment. That matters more than it
sounds: Playwright *is* installed in this test environment, so
`can_deliver_in_process()` is True by default and the entire suite runs with
the gate open. A test that forgets to close it exercises nothing.

What used to happen: `start_drainer` was ungated at five of its six call sites,
so the serverless web process claimed rows into `sending` whether or not it
could finish them. Once the claim became atomic exactly one drainer wins, and
the incapable one can win — `worker.py`, the only host that can drive a
browser, then finds nothing queued and moves on. The message waits out
`reclaim_stuck_sending` (900s) plus the next agent pass: up to ~20 minutes.

The gate lives in `automation.start_drainer` rather than at the routes on
purpose, and one test below is the reason why: `/responder/send` — the primary
send path — never calls `start_drainer` itself. It reaches it through
`automation.enqueue_send`. A gate written at the two filed dashboard routes
would have left the main path still claiming rows, and read as done.

`worker.py` is unaffected by construction: it never calls `start_drainer`, it
drives `automation.send_next` directly in its own loop.

All six call sites funnel through the one gate, so they are covered by
`test_an_incapable_process_starts_no_drainer` rather than by six near-identical
tests. The two that get their own test —  `enqueue_send` and
`enqueue_autopilot_reply` — earn it by reaching the drainer *transitively*,
which is the property a future refactor could break without touching
`start_drainer` at all.
"""
import os
import tempfile
import threading

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


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ven149.db")
    monkeypatch.setattr(pipeline, "_TS_NORMALIZED", False, raising=False)
    tid = "1"
    config.save_settings(tid, host_name="Test Host", timezone="America/New_York")
    return tid


@pytest.fixture(autouse=True)
def _no_drainer_leaks():
    """`_draining` is module global. A test that legitimately starts a drainer
    would otherwise make the *next* test's `start_drainer` return False for the
    wrong reason — idempotency, not the gate — and quietly pass."""
    automation._draining = False
    yield
    automation._draining = False


@pytest.fixture()
def incapable(monkeypatch):
    """The worker-queue topology: this process must not claim rows."""
    monkeypatch.setenv("FORCE_WORKER_QUEUE", "1")
    assert not automation.can_deliver_in_process(), (
        "the gate did not actually close, so nothing below tests anything")


def _deal(tenant_id, item_id="s1", guest="Dana R."):
    item = {"id": item_id, "kind": "lead", "traveler": guest,
            "title": f"Unit | {guest}", "property_name": ""}
    storage.filter_new(tenant_id, SITE, "lead", [item])
    pipeline.ensure(tenant_id, SITE, item, None)
    return item


def _drainer_threads():
    return [t for t in threading.enumerate() if t.name == "outbox-drainer"]


# ---------------------------------------------------------------------------
# The claim itself
# ---------------------------------------------------------------------------


def test_an_incapable_process_starts_no_drainer(incapable):
    """The whole fix in one assertion, at the single point every path funnels
    through."""
    before = len(_drainer_threads())
    assert automation.start_drainer(SITE) is False
    assert automation._draining is False, (
        "the drainer flag was set, so a later capable pass in this process "
        "would be told a drainer is already running when none is")
    assert len(_drainer_threads()) == before, "an incapable host spawned a drainer"


def test_a_capable_process_still_starts_one(tenant, monkeypatch):
    """Acceptance criterion 2: single-host in-process delivery is unchanged.

    Asserted positively rather than left to the rest of the suite, so a gate
    that closes on *every* host — the obvious way to break this — fails here
    instead of silently disabling delivery for single-host installs.
    """
    monkeypatch.delenv("FORCE_WORKER_QUEUE", raising=False)
    monkeypatch.setattr(automation, "_drain_loop", lambda site: None)
    assert automation.can_deliver_in_process() is True
    assert automation.start_drainer(SITE) is True


def test_responder_send_does_not_claim_the_row(tenant, incapable):
    """The call site a route-level fix would have missed.

    `/responder/send` is the primary send path and it does not mention
    `start_drainer` anywhere — it calls `automation.enqueue_send`, which calls
    it. The ticket named only the two dashboard routes; this is the one that
    matters most.
    """
    _deal(tenant)
    msg = automation.enqueue_send(tenant, SITE, "s1", "Hi there!")

    assert msg is not None
    assert outbox.get(msg["id"])["status"] == outbox.QUEUED, (
        "an incapable host claimed the row it cannot deliver; worker.py will "
        "now find nothing queued and the guest waits out the reclaim window")
    assert not _drainer_threads()


def test_autopilot_reply_does_not_claim_the_row(tenant, incapable, monkeypatch):
    """`enqueue_autopilot_reply` is the third `start_drainer` call site in
    automation.py, reached with no dashboard in the stack at all."""
    _deal(tenant)
    monkeypatch.setattr(automation, "settings_for",
                        lambda tid: {"enabled": True, "steps": {"intro"}})
    monkeypatch.setattr(automation.sequences, "can_auto_send",
                        lambda step, allowed: True)

    msg = automation.enqueue_autopilot_reply(tenant, SITE, "s1", "Hello!")

    assert msg is not None
    assert outbox.get(msg["id"])["status"] == outbox.QUEUED
    assert not _drainer_threads()


def test_queued_is_a_recoverable_resting_state(tenant):
    """The *premise* of the trade this fix makes, pinned separately.

    Declining the claim is only the better failure mode because a `queued` row
    is cancelable by the operator and collectable by any capable host. If
    either stopped being true, the gate above would silently become a way to
    strand messages rather than defer them — and every gate test would still
    pass, because they assert the row is `queued`, not that `queued` is worth
    being.

    Deliberately *not* a gate regression test, and it does not go through
    `enqueue_send`: it builds the row directly so no drainer thread exists to
    race with. An earlier version did use `enqueue_send` and asserted only
    `status in CANCELABLE`, which passed against a tree with the gate removed
    — the drainer had claimed the row, the send failed without Playwright, and
    `failed` is also in CANCELABLE. It was green for the opposite of the
    reason it claimed.
    """
    _deal(tenant)
    msg = outbox.add(tenant, SITE, "s1", sequence="presale", step_id="intro",
                     step_label="First reply", body="Hi there!", auto=True)

    row = outbox.get(msg["id"])
    assert row["status"] == outbox.QUEUED
    assert row["status"] in outbox.CANCELABLE, (
        "the row an incapable host declined to claim is not cancelable, so the "
        "operator has no route to call it off")
    assert outbox.next_queued(tenant, SITE) is not None, (
        "a capable host would not pick this row up, so declining the claim "
        "stranded it instead of deferring it")


# ---------------------------------------------------------------------------
# Q1: the misconfiguration must stay visible
# ---------------------------------------------------------------------------


def test_no_capable_sender_fires_when_nothing_can_send(tenant, incapable,
                                                       monkeypatch):
    """Operator decision Q1 (`visible_surface`): gating made a broken deploy
    quiet. Before the gate an approved message failed fast and said so; after
    it the row correctly stays `queued` and the card says "Queued to send…"
    forever. `queued` is the right state; silence about it is not."""
    import dashboard
    import jobs

    monkeypatch.setattr(jobs, "worker_online", lambda: False)
    assert dashboard._no_capable_sender(queued=1) is True


def test_no_capable_sender_is_silent_when_a_worker_is_alive(tenant, incapable,
                                                            monkeypatch):
    """A healthy worker-queue deployment is the *normal* case for an incapable
    web process. If the banner fired there it would be permanent wallpaper and
    would stop meaning anything."""
    import dashboard
    import jobs

    monkeypatch.setattr(jobs, "worker_online", lambda: True)
    assert dashboard._no_capable_sender(queued=1) is False


def test_no_capable_sender_is_silent_with_nothing_queued(tenant, incapable,
                                                         monkeypatch):
    import dashboard
    import jobs

    monkeypatch.setattr(jobs, "worker_online", lambda: False)
    assert dashboard._no_capable_sender(queued=0) is False


def test_no_capable_sender_is_silent_on_a_capable_host(tenant, monkeypatch):
    import dashboard
    import jobs

    monkeypatch.delenv("FORCE_WORKER_QUEUE", raising=False)
    monkeypatch.setattr(jobs, "worker_online", lambda: False)
    assert dashboard._no_capable_sender(queued=5) is False, (
        "a single-host install with no separate worker is not misconfigured — "
        "it delivers in-process, and must never see this banner")


def test_the_banner_never_breaks_the_render(tenant, incapable, monkeypatch):
    """A liveness lookup is a DB read and can fail. It must not take the
    dashboard down with it."""
    import dashboard
    import jobs

    def boom():
        raise RuntimeError("worker table unavailable")

    monkeypatch.setattr(jobs, "worker_online", boom)
    with dashboard.app.test_request_context():
        assert dashboard._no_capable_sender(queued=1) is False


def test_board_reports_the_stall_to_the_template(tenant, incapable, monkeypatch):
    """Driven through `_board`, because a helper nobody calls is not a surface.

    This is the assertion that would have caught the banner being computed and
    then dropped on the floor before reaching the template context.
    """
    import dashboard
    import jobs

    monkeypatch.setattr(jobs, "worker_online", lambda: False)
    _deal(tenant)
    automation.enqueue_send(tenant, SITE, "s1", "Hi there!")

    with dashboard.app.test_request_context():
        board = dashboard._board(tenant)

    assert board["outbox_counts"]["queued"] == 1
    assert board["no_capable_sender"] is True, (
        "the dashboard renders a queued message with no process alive to send "
        "it and says nothing about it")


# ---------------------------------------------------------------------------
# Q2: gating the scheduler must not disable autopilot on the worker topology
# ---------------------------------------------------------------------------


def test_autopilot_stays_reachable_through_the_shared_db(tenant, incapable):
    """The regression guard on the Q2 decision.

    The question put to the operator claimed that gating `start_scheduler`
    would make switching autopilot on "silently do nothing" on an incapable
    host. That premise is false, and this test is what makes it stay false:
    the toggle's durable effect is the settings write, and `worker.py` reads
    that same row (`run_scheduled_checks` -> `scheduler.due_tenants` ->
    `is_on` -> `config.get_settings`). Refusing the toggle — the option that
    premise led to — would have made autopilot unreachable on the one
    deployment topology it is documented for.
    """
    import scheduler

    config.save_settings(tenant, autopilot="1")
    assert scheduler.is_on(tenant) is True, (
        "the autopilot setting did not survive the write, so worker.py cannot "
        "see it and gating the in-process scheduler really would disable the "
        "feature")
