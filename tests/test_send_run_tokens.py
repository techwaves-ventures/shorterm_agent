"""VEN-130: a send may only be recorded `sent` on its *own* terminal state.

`runner._state` is one process-global slot. Before this change the only thing
`automation.send_next` could ask it was "is anything running?", and the answer
was about whichever run last touched the slot — not about the run `send_reply`
had just started for this row. So when a send's own terminal state was replaced
before a 2-second poll observed it, the loop read the replacement's outcome as
this row's: a scrape finishing cleanly marked a *failed* send `sent`, fired
`after_contact`, and advanced the follow-up cadence past a guest who was never
written to.

The fix is an opaque per-invocation token handed back by `send_reply` and worn
by every state that send writes. Two directions have to hold, and only one of
them is the happy path:

* a matching terminal is accepted — and
* a NON-matching terminal is not, no matter how successful it looks.

The second is the whole ticket. A suite that only proves the first passes
unchanged against the defect, which is why the scripted-snapshot tests below
enumerate the mismatches explicitly rather than asserting one good send.

Red-on-base status, verified against `0f0100e`:

* every scripted `send_next` test and the two runner ownership tests fail on
  the unfixed code for the filed reason (a row recorded `sent`, or a stale
  worker clobbering a newer run);
* the token-shape tests fail on base because the field does not exist there.

Nothing here touches FurnishedFinder, email, credentials or real guest data:
the browser seam is stubbed and the database is a per-test temp file.
"""
import os
import tempfile
import threading
import types

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("FF_CRED_KEY", "c9jwUi0L-fUjf3wjbq74M0lK3ah7fmEfGhjxZ7RehQk=")
os.environ.setdefault("SECRET_KEY", "test-secret")

import automation  # noqa: E402
import config  # noqa: E402
import outbox  # noqa: E402
import pipeline  # noqa: E402
import runner  # noqa: E402
import storage  # noqa: E402

SITE = "furnishedfinder"


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ven130.db")
    monkeypatch.setattr(pipeline, "_TS_NORMALIZED", False, raising=False)
    tid = "1"
    config.save_settings(tid, host_name="Test Host", timezone="America/New_York")
    return tid


@pytest.fixture(autouse=True)
def _clean_runner_state():
    """`runner._state` is process-global and these tests write to it directly.
    Restore it around every test so a failure here cannot turn an unrelated
    test in the same run red."""
    with runner._lock:
        before = dict(runner._state)
    yield
    with runner._lock:
        runner._state.clear()
        runner._state.update(before)


def _deal(tenant_id, item_id, *, kind="lead", guest="Dana R."):
    item = {"id": item_id, "kind": kind, "traveler": guest,
            "title": f"Unit | {guest}", "property_name": ""}
    storage.filter_new(tenant_id, SITE, kind, [item])
    pipeline.ensure(tenant_id, SITE, item, None)
    return item


def _queued(tenant_id, item_id="s1", body="hello"):
    _deal(tenant_id, item_id)
    msg = outbox.add(tenant_id, SITE, item_id, sequence="presale",
                     step_id="intro", step_label="First reply",
                     body=body, auto=True)
    outbox.set_status(msg["id"], outbox.QUEUED)
    return msg


def _idle():
    """Put the runner back in the state a fresh process starts in."""
    with runner._lock:
        runner._state.update(status="idle", message="", counts={}, running=False,
                             tenant_id=None, kind=None, run_token=None)


def _stub_browser(monkeypatch):
    """Neutralise the FurnishedFinder seam so `_send_worker` can run for real
    without a browser. Channels are emptied, so the worker takes its normal
    success path straight to the terminal write."""
    monkeypatch.setattr(runner, "_channels", lambda _t: set())
    monkeypatch.setattr(runner.furnishedfinder, "set_context", lambda *a, **k: None)
    monkeypatch.setattr(runner.furnishedfinder, "clear_context", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# The token itself: present exactly when a thread was dispatched
# ---------------------------------------------------------------------------


def test_an_accepted_send_returns_a_token_and_a_busy_one_does_not(tenant, monkeypatch):
    """The token is the caller's proof that a thread was started *for it*.

    Both halves are asserted together on purpose. "busy carries no token" is
    vacuously true on the unfixed code, where nothing carries one — so on its
    own it is not evidence of anything. The accepted call is the control that
    makes the negative mean something.
    """
    dispatched = []
    monkeypatch.setattr(runner, "_send_worker",
                        lambda *a: dispatched.append(a))
    _idle()

    accepted = runner.send_reply(tenant, SITE, {"id": "s1", "kind": "lead"}, "hi")
    assert accepted.get("run_token"), "an accepted send must be correlatable"
    assert len(dispatched) == 1, "an accepted send starts exactly one thread"

    # Now the runner is busy with someone else's run.
    with runner._lock:
        runner._state.update(status="checking", running=True,
                             tenant_id=str(tenant), kind="scrape", run_token=None)
    busy = runner.send_reply(tenant, SITE, {"id": "s2", "kind": "lead"}, "hi")

    assert busy.get("status") == "busy"
    assert not busy.get("run_token"), (
        "nothing was dispatched, so there is no run to correlate with — a token "
        "here would let the caller wait for, and adopt, a stranger's outcome")
    assert len(dispatched) == 1, "a collision must start no second thread"


@pytest.mark.parametrize("holder", ["1", "2"], ids=["same tenant", "other tenant"])
def test_no_collision_hands_back_a_usable_token(tenant, monkeypatch, holder):
    """Same-tenant and cross-tenant collisions both dispatch nothing, so
    neither may look accepted. The same-tenant case is the one that produced
    the original false `sent` (VEN-127), so it is pinned on both axes."""
    dispatched = []
    monkeypatch.setattr(runner, "_send_worker", lambda *a: dispatched.append(a))
    with runner._lock:
        runner._state.update(status="checking", running=True,
                             tenant_id=holder, kind="scrape", run_token=None)

    state = runner.send_reply("1", SITE, {"id": "s1", "kind": "lead"}, "hi")

    assert state.get("status") == "busy"
    assert not state.get("run_token")
    assert dispatched == []

    # Control: the same call against a free runner does hand back a token, so
    # the absence above is the collision's doing and not the absence of the
    # mechanism.
    _idle()
    accepted = runner.send_reply("1", SITE, {"id": "s1", "kind": "lead"}, "hi")
    assert accepted.get("run_token") and len(dispatched) == 1


def test_a_busy_state_never_echoes_the_in_flight_sends_token(tenant, monkeypatch):
    """The collision tests above have a *scrape* holding the runner, which owns
    no token — so they cannot see whether `_busy_state` would hand one over.
    Put a real send in the slot instead.

    `_busy_state` exists precisely so a blocked caller is told nothing about the
    run that blocked it; the token is the strongest thing on that state, and it
    would go to whoever collided, including another tenant.
    """
    monkeypatch.setattr(runner, "_send_worker", lambda *a: None)
    _idle()
    in_flight = runner.send_reply("1", SITE, {"id": "s1", "kind": "lead"}, "hi")
    assert in_flight.get("run_token"), "control: a send is holding the runner"

    for other in ("1", "2"):
        blocked = runner.send_reply(other, SITE, {"id": "s2", "kind": "lead"}, "hi")
        assert blocked.get("status") == "busy"
        assert blocked.get("run_token") != in_flight["run_token"], (
            f"tenant {other} was handed the in-flight send's identity")
        assert not blocked.get("run_token")


def test_send_reply_reports_its_own_token_not_the_slots(tenant, monkeypatch):
    """`send_reply` returns *after* starting the thread, so between the two the
    worker can finish and the next run can claim the slot. Reading the token
    back out of the shared state at that point hands the caller the *next*
    run's identity — the same class of bug one level up, and one that would
    make every guard below it agree on the wrong run.

    The thread is run inline so that interleaving is forced rather than raced.
    """
    class _InlineThread:
        def __init__(self, target=None, args=(), **_kw):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    inline = types.SimpleNamespace(Lock=threading.Lock, Event=threading.Event,
                                   Thread=_InlineThread)
    monkeypatch.setattr(runner, "threading", inline)

    def worker(*_args):
        # This send finishes and the next one claims the slot, both before
        # `send_reply` gets to read the state back.
        with runner._lock:
            runner._state.update(status="done", running=False)
            runner._state.update(status="launching", running=True, kind="send",
                                 tenant_id="1", run_token="the-next-run")

    monkeypatch.setattr(runner, "_send_worker", worker)
    _idle()

    state = runner.send_reply("1", SITE, {"id": "s1", "kind": "lead"}, "hi")

    assert state.get("run_token"), "the dispatch happened, so it is correlatable"
    assert state["run_token"] != "the-next-run", (
        "send_reply handed back the run that replaced it")


def test_two_accepted_sends_never_share_a_token(tenant, monkeypatch):
    """The token identifies an invocation, not a tenant and not a row. Reusing
    one would put the second send back where the bug was: able to adopt the
    first's outcome."""
    monkeypatch.setattr(runner, "_send_worker", lambda *a: None)
    seen = set()
    for i in range(5):
        _idle()
        token = runner.send_reply(tenant, SITE, {"id": f"s{i}", "kind": "lead"},
                                  "hi").get("run_token")
        assert token, "every accepted send is correlatable"
        seen.add(token)
    assert len(seen) == 5, "each accepted invocation gets its own token"


def test_a_scrape_never_wears_the_previous_sends_token(tenant, monkeypatch):
    """A scrape is not a send. If `start_scrape` left the last send's token in
    place, the scrape's own `done` would answer that send's poller — which is
    precisely the false-`sent` path, just arriving from the other side."""
    monkeypatch.setattr(runner, "_send_worker", lambda *a: None)
    monkeypatch.setattr(runner, "_worker", lambda *a: None)
    _idle()

    token = runner.send_reply(tenant, SITE, {"id": "s1", "kind": "lead"}, "hi").get("run_token")
    assert token, "control: the send really did mint a token"
    with runner._lock:                      # the send's thread finishes
        runner._state.update(status="done", running=False)

    runner.start_scrape(tenant)

    assert runner.get_state(tenant).get("run_token") is None, (
        "the scrape inherited the send's identity")


def test_the_cross_tenant_idle_snapshot_never_echoes_a_token(tenant, monkeypatch):
    """`get_state`'s tenant leak guard returns a synthetic idle snapshot. It
    must not carry the running tenant's token either — a token is as much a
    cross-run authority here as the status message is a privacy leak."""
    monkeypatch.setattr(runner, "_send_worker", lambda *a: None)
    _idle()
    token = runner.send_reply("1", SITE, {"id": "s1", "kind": "lead"}, "hi").get("run_token")
    assert token, "control: without a real token the comparisons below are None == None"

    assert runner.get_state("1").get("run_token") == token, (
        "control: the owning tenant does see its own token")
    assert runner.get_state("2").get("run_token") is None, (
        "another tenant's snapshot must not carry this run's token")


# ---------------------------------------------------------------------------
# The worker: it writes only while it still owns the slot
# ---------------------------------------------------------------------------


def test_the_token_survives_to_the_terminal_state(tenant, monkeypatch):
    """The whole mechanism is worthless if the token is dropped somewhere
    between dispatch and the terminal write, so follow one real send through
    `_send_worker` and check the terminal state still wears it."""
    _stub_browser(monkeypatch)
    _deal(tenant, "s1")
    _idle()
    done = threading.Event()
    monkeypatch.setattr(runner.furnishedfinder, "clear_context",
                        lambda *a, **k: done.set())

    token = runner.send_reply(tenant, SITE, {"id": "s1", "kind": "lead"},
                              "hi").get("run_token")
    assert token, "control: without a real token the comparison below is None == None"
    assert done.wait(10), "the send worker never finished"

    snap = runner.get_state(tenant)
    assert snap.get("status") == "done" and snap.get("running") is False
    assert snap.get("run_token") == token, (
        "a terminal state with no token is unattributable, and `send_next` "
        "must then fail closed rather than guess")


def test_a_stale_worker_cannot_write_over_a_newer_run(tenant, monkeypatch):
    """The inverse of the poll-side guard, and the reason the guard is worth
    anything.

    If a worker whose run has already been replaced still writes its terminal
    `done` and clears `running`, the newer run's token stays on the row — so the
    newer run's poller reads the *stale* worker's outcome as its own, with a
    matching token, and the token check waves it through. Ownership has to be
    enforced on the write as well as the read.
    """
    _stub_browser(monkeypatch)
    _deal(tenant, "s1")
    _idle()
    inside = threading.Event()
    release = threading.Event()
    done = threading.Event()
    monkeypatch.setattr(runner.furnishedfinder, "clear_context",
                        lambda *a, **k: done.set())

    real_update = storage.update_response

    def slow_update(*a, **k):
        inside.set()
        release.wait(10)
        return real_update(*a, **k)

    monkeypatch.setattr(runner.storage, "update_response", slow_update)

    runner.send_reply(tenant, SITE, {"id": "s1", "kind": "lead"}, "hi")
    assert inside.wait(10), "the send worker never reached its terminal write"

    # A newer run takes the slot while the first worker is still in flight.
    with runner._lock:
        runner._state.update(status="checking", message="Starting…", running=True,
                             tenant_id=str(tenant), kind="scrape", run_token=None)
    release.set()
    assert done.wait(10), "the stale worker never finished"

    snap = runner.get_state(tenant)
    assert snap.get("running") is True, (
        "the stale worker cleared the newer run's `running`; its poller now "
        "sees a terminal state it never earned")
    assert snap.get("status") != "done", (
        "the stale worker published its outcome as the newer run's")
    assert snap.get("run_token") is None


# ---------------------------------------------------------------------------
# send_next: which terminal states may record an outcome
# ---------------------------------------------------------------------------


class _ScriptedRunner:
    """Stands in for `runner` so the acceptance rule can be pinned exactly.

    The real runner cannot produce these interleavings on demand — that is the
    point of the bug, it depends on which of two runs touched a shared slot
    last. Scripting the snapshots removes the race from the test without
    removing it from the thing being tested: `send_next` still makes the same
    decision from the same inputs.
    """

    def __init__(self, accepted, snapshots):
        self.accepted = accepted
        self.snapshots = list(snapshots)
        self.polls = 0

    def send_reply(self, *a, **k):
        return dict(self.accepted)

    def get_state(self, tenant_id=None):
        i = min(self.polls, len(self.snapshots) - 1)
        self.polls += 1
        return dict(self.snapshots[i])


def _terminal(status, token, message=""):
    return {"status": status, "message": message, "counts": {}, "running": False,
            "tenant_id": "1", "kind": "send", "run_token": token,
            "updated_at": "2026-09-04T00:00:00"}


class _FastClock:
    """`automation`'s view of `time`, on a virtual clock.

    Substituted for the module in `automation`'s namespace rather than patching
    `time.sleep` itself, which would reach every other thread in the process.
    The poll cadence and the timeout are then exact instead of wall-clock
    approximate: `timeout=1` is one poll, not "however many fit in a second".
    """

    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture()
def scripted(monkeypatch):
    """Install a scripted runner and neuter the poll delay and the outbound
    notification. Returns a setup callable plus the recorded side effects."""
    monkeypatch.setattr(automation, "time", _FastClock())
    contacted = []
    notified = []
    monkeypatch.setattr(automation, "after_contact",
                        lambda *a, **k: contacted.append(a))
    monkeypatch.setattr(automation, "_notify_failure",
                        lambda *a, **k: notified.append(a))

    def install(accepted, snapshots):
        stub = _ScriptedRunner(accepted, snapshots)
        monkeypatch.setattr(runner, "send_reply", stub.send_reply)
        monkeypatch.setattr(runner, "get_state", stub.get_state)
        return stub

    return install, contacted, notified


def test_a_matching_done_is_recorded_sent(tenant, scripted):
    """The happy path. It passes on the unfixed code too — that is exactly why
    it is not evidence on its own, and why the tests below exist."""
    install, contacted, _ = scripted
    msg = _queued(tenant)
    install({"status": "launching", "running": True, "run_token": "A"},
            [_terminal("done", "A", "Reply sent to Dana R.")])

    automation.send_next(tenant, SITE, timeout=30)

    assert outbox.get(msg["id"])["status"] == outbox.SENT
    assert len(contacted) == 1, "the follow-up cadence advances exactly once"


def test_a_matching_error_is_recorded_failed(tenant, scripted):
    install, contacted, notified = scripted
    msg = _queued(tenant)
    install({"status": "launching", "running": True, "run_token": "A"},
            [_terminal("error", "A", "Send failed: browser crashed")])

    automation.send_next(tenant, SITE, timeout=30)

    row = outbox.get(msg["id"])
    assert row["status"] == outbox.FAILED
    assert "browser crashed" in (row["error"] or "")
    assert contacted == [], "a failed send must not advance the lifecycle"
    assert len(notified) == 1


def test_another_runs_done_is_ignored_and_this_runs_error_is_honoured(tenant, scripted):
    """The filed defect, in the order it actually happens.

    This send fails. Before `send_next`'s next poll, a scrape finishing for the
    same tenant replaces the shared state with a clean `done`. On the unfixed
    code that snapshot is the one the loop reads: the row is recorded `sent`,
    `after_contact` fires, and the cadence moves past a guest who was never
    written to. The stale terminal has to be ignored, and this run's own error
    honoured when it arrives.
    """
    install, contacted, notified = scripted
    msg = _queued(tenant)
    install(
        {"status": "launching", "running": True, "run_token": "A"},
        [
            # A scrape's terminal state — a real success, belonging to a run
            # this row never started.
            _terminal("done", None, "Done."),
            _terminal("done", None, "Done."),
            # This send's own outcome, arriving late.
            _terminal("error", "A", "Send failed: reply box not found"),
        ],
    )

    automation.send_next(tenant, SITE, timeout=30)

    row = outbox.get(msg["id"])
    assert row["status"] == outbox.FAILED, (
        "a terminal state from another run was adopted as this send's outcome")
    assert "reply box not found" in (row["error"] or ""), (
        "the row must record why THIS send failed")
    assert contacted == [], (
        "`after_contact` on an undelivered message advances the follow-up "
        "cadence past a guest who was never written to")
    assert len(notified) == 1


@pytest.mark.parametrize(
    "snapshot, why",
    [
        (_terminal("done", "B"), "a different send's success"),
        (_terminal("done", None), "a scrape's success"),
        (_terminal("done", ""), "an empty token is not a match"),
        (_terminal("idle", "A"), "`idle` is not an outcome this send reported"),
        (_terminal("busy", "A"), "`busy` means nothing was dispatched"),
        (_terminal("launching", "A"), "a run that never reached a terminal state"),
        (dict(_terminal("done", "A"), running=True),
         "`done` while still running is not a finished send"),
    ],
    ids=["other send", "scrape", "empty token", "idle", "busy", "launching",
         "done but running"],
)
def test_no_unmatched_terminal_can_ever_record_sent(tenant, scripted, snapshot, why):
    """Six ways a not-running snapshot can turn up that are not this send's
    outcome. Every one of them used to be recorded `sent`: the old loop asked
    only `not running`, then treated anything that was not `error` as success.

    The row must end up failed by the timeout, never sent.
    """
    install, contacted, notified = scripted
    msg = _queued(tenant)
    install({"status": "launching", "running": True, "run_token": "A"}, [snapshot])

    automation.send_next(tenant, SITE, timeout=1)

    row = outbox.get(msg["id"])
    assert row["status"] != outbox.SENT, f"{why}: recorded as delivered"
    assert row["status"] == outbox.FAILED, f"{why}: must reach the timeout path"
    assert contacted == [], f"{why}: the lifecycle advanced on it"
    assert len(notified) == 1


def test_an_accepted_dispatch_without_a_token_fails_closed(tenant, scripted):
    """Belt and braces for the one case the token cannot resolve.

    If a dispatch ever comes back accepted but uncorrelatable, there is no
    snapshot that could prove delivery, so waiting for one is pointless. Fail
    closed and say why: a wrongly-failed row is retried and the operator is
    told, while a wrongly-`sent` one strands the guest in silence.
    """
    install, contacted, notified = scripted
    msg = _queued(tenant)
    stub = install({"status": "launching", "running": True},
                   [_terminal("done", None, "Done.")])

    automation.send_next(tenant, SITE, timeout=30)

    row = outbox.get(msg["id"])
    assert row["status"] == outbox.FAILED
    assert "correlate" in (row["error"] or "")
    assert contacted == []
    assert len(notified) == 1
    assert stub.polls == 0, "there is nothing worth polling for"


def test_a_busy_collision_still_leaves_the_row_queued_with_its_budget(tenant, scripted):
    """VEN-127's refund behaviour, re-pinned here because VEN-130 rewrites the
    lines immediately below it. Nothing was dispatched, so the row goes back on
    the queue with its retry budget intact — it must not be spent on a send
    that never happened."""
    install, contacted, notified = scripted
    msg = _queued(tenant)
    install({"status": "busy", "running": False, "run_token": None},
            [_terminal("done", None, "Done.")])

    assert automation.send_next(tenant, SITE, timeout=30) is None

    row = outbox.get(msg["id"])
    assert row["status"] == outbox.QUEUED
    assert row["attempts"] < outbox.MAX_SEND_ATTEMPTS
    assert contacted == [] and notified == []
