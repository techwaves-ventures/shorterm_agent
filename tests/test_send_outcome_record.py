"""VEN-215: a send's outcome belongs to the send, not to whoever holds the slot.

VEN-130 gave every send an opaque per-run token so `automation.send_next` could
tell its *own* dispatch's terminal state from another run's. That fixed
attribution. It did not fix durability: the outcome was still published to the
one process-global `runner._state` slot, and the poller has no memory of an
outcome it did not happen to observe inside its two-second tick.

So the harm survived, with its sign flipped. The send worker really drives the
browser, `furnishedfinder.send_reply` really writes to the guest, and the worker
writes its `done`. A scrape claims the runner in the poll gap — `start_scrape`
nulls the token by design, because a scrape must never wear a send's identity —
and the send's own poller now sees a terminal state that is not its own, waits
out the clock, and records the row `failed` / "timed out waiting for the send to
finish", notifies the operator, and offers a primary **Retry send** beside a
message the guest already received.

Reachability is per-process, not per-repo: closed in `worker.py`, where the
scrape pass and the drain pass are sequential in one thread; open in the
dashboard, where `start_scheduler` and `start_drainer` are concurrent threads
and "Check now" is one click.

The fix records each send's terminal outcome under its own token and has the
poller read that back before falling to the live slot. It does not widen the
acceptance rule: the done/error set, the not-running condition and the
no-token fail-closed branch are all unchanged, and a record keyed by an
unguessable per-invocation token can only ever answer with its own run's
outcome.

Red-on-base status, measured against `c06f9be` (the head of PR #43 — this
branch's base, not `main`): all 14 fail. Seven of them fail on *behaviour*, and
those are the ones that are evidence:

* `test_a_delivered_send_is_recorded_sent_even_if_a_scrape_takes_the_slot`
  — "a delivered send was recorded failed: 'timed out waiting for the send to
  finish'", which is the ticket verbatim;
* `test_a_failed_send_is_recorded_with_its_own_error_not_the_timeout`
  — the worker's own "reply box not found" never reaches the row;
* the two wording cases — the stored error and the operator notification carry
  no warning, asserted as a literal sentence so the red is the missing warning
  and not a missing constant;
* the three seam tests — `/api/status`, `/v1/state` and `/v1/login` really do
  serve the live token.

The other seven pin the contract of an API that does not exist on base, so they
fail there on the missing name (`runner._publish_terminal`,
`runner._OUTCOMES_MAX`, `runner.take_send_outcome`,
`automation.MAY_HAVE_REACHED_GUEST`). That is worth being explicit about rather
than quoting "14 red" and letting it read as fourteen reproductions of the
defect. In particular
`test_another_runs_recorded_outcome_is_never_adopted` is the no-regression
direction: it is not a base differential at all, it is here to fail if a later
change buys durability by loosening the acceptance rule VEN-130 tightened.

Nothing here touches FurnishedFinder, email, credentials or real guest data: the
browser seam is stubbed, the auth seam is signed with the test key, and the
database is a per-test temp file. Fixture ids are namespaced `v215-*` because
`db.DB_PATH` resolves once at import, so in a whole-suite run every module lands
in one database and a colliding id turns a sibling file red.
"""
import contextlib
import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
import time as _real_time
import types

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("FF_CRED_KEY", "c9jwUi0L-fUjf3wjbq74M0lK3ah7fmEfGhjxZ7RehQk=")
os.environ.setdefault("SECRET_KEY", "test-secret")
# Set before `browser_server` is imported: `SignatureAuth` captures these at
# import time and fails closed without them. `setdefault`, so whichever of this
# file and `test_browser_server.py` is imported first in a whole-suite run wins
# and the other still signs with the right key (read back off the module below,
# never hardcoded here).
os.environ.setdefault("BROWSER_SERVER_BEARER_TOKEN", "test-bearer-token")
os.environ.setdefault("BROWSER_SERVER_HMAC_KEY", "test-hmac-key")

import automation  # noqa: E402
import browser_server  # noqa: E402
import config  # noqa: E402
import ff_account  # noqa: E402
import outbox  # noqa: E402
import pipeline  # noqa: E402
import runner  # noqa: E402
import storage  # noqa: E402

SITE = "furnishedfinder"
EMAIL = "v215-host@example.com"
PASSWORD = "a-perfectly-fine-passphrase"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    """A logged-in-able tenant with a connected FF account.

    The FF connection is not decoration: without it `scrape_allowed` 403s the
    status/refresh/otp routes and the template renders the verification note
    instead of the board — so every "the token is absent" assertion in T8 would
    pass over a page that has nothing on it at all.
    """
    import db
    import models

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ven215.db")
    monkeypatch.setattr(pipeline, "_TS_NORMALIZED", False, raising=False)
    monkeypatch.setenv("INSECURE_COOKIES", "1")

    user = models.create_user(EMAIL, PASSWORD)
    tid = str(user.tenant_id)
    config.save_settings(tid, host_name="Test Host", timezone="America/New_York",
                         onboarded="1")
    ff_account.connect(tid, "v215-ff@example.com")
    ff_account.mark_state(tid, "connected")
    return tid


@pytest.fixture(autouse=True)
def _clean_runner_globals():
    """`runner._state` and `runner._outcomes` are both process-global and these
    tests write to both. Restore them around every test, or a failure here turns
    an unrelated test in the same run red. `test_send_run_tokens.py` restores
    `_state` only, because `_outcomes` did not exist when it was written."""
    with runner._lock:
        state_before = dict(runner._state)
        outcomes_before = dict(getattr(runner, "_outcomes", {}))
    yield
    with runner._lock:
        runner._state.clear()
        runner._state.update(state_before)
        if hasattr(runner, "_outcomes"):
            runner._outcomes.clear()
            runner._outcomes.update(outcomes_before)


def _idle():
    """Put the runner back in the state a fresh process starts in."""
    with runner._lock:
        runner._state.update(status="idle", message="", counts={}, running=False,
                             tenant_id=None, kind=None, run_token=None)


class _InlineThread:
    """A `Thread` that runs its target on `start()`.

    The interleaving this file is about is not schedulable on demand — that is
    the whole nature of the bug. Running the worker inline turns it into an
    ordering the test states outright, without changing a line of the code that
    makes the decision.
    """

    def __init__(self, target=None, args=(), **_kw):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _inline_threads(monkeypatch):
    monkeypatch.setattr(runner, "threading", types.SimpleNamespace(
        Lock=threading.Lock, Event=threading.Event, Thread=_InlineThread))


def _stub_platform_send(monkeypatch, on_send):
    """Neutralise the FurnishedFinder seam, keeping the *platform* channel on so
    `_send_worker` takes its real path through `furnishedfinder.send_reply`.

    `on_send` stands where the guest is written to. Its being called is the
    premise of the whole ticket: a send that never reached anyone being recorded
    failed would be correct, not a bug.
    """
    monkeypatch.setattr(runner, "_channels", lambda _t: {"platform"})
    monkeypatch.setattr(runner.furnishedfinder, "set_context", lambda *a, **k: None)
    monkeypatch.setattr(runner.furnishedfinder, "clear_context", lambda *a, **k: None)
    monkeypatch.setattr(runner.furnishedfinder, "send_reply", on_send)

    @contextlib.contextmanager
    def fake_page(_tenant_id):
        yield object()

    monkeypatch.setattr(runner.check_leads, "browser_page", fake_page)


def _scrape_claims_the_slot_when_the_send_returns(monkeypatch, tenant_id):
    """Model the filed interleaving: this send finishes, and a scrape claims and
    releases the runner before the send's poller looks.

    The scrape is the real `start_scrape` and the real `_set` terminal write, so
    the token-nulling that defeats the poller is the shipped code's doing, not a
    hand-written slot write. The competing run must *finish*: a competing run
    that merely claims the slot and hangs stalls both heads and proves nothing
    while reading exactly like a confirmed regression.
    """
    monkeypatch.setattr(runner, "_worker", lambda _t: runner._set(
        status="done", message="Done.", counts={"leads": 0}, running=False))
    real_send_reply = runner.send_reply

    def dispatch_then_scrape(*a, **k):
        state = real_send_reply(*a, **k)
        runner.start_scrape(tenant_id)
        return state

    monkeypatch.setattr(runner, "send_reply", dispatch_then_scrape)


class _FastClock:
    """`automation`'s view of `time`, on a virtual clock, so `timeout=1` means
    exactly one poll rather than however many fit in a second."""

    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _deal(tenant_id, item_id, *, guest="Dana R."):
    item = {"id": item_id, "kind": "lead", "traveler": guest,
            "title": f"Unit | {guest}", "property_name": ""}
    storage.filter_new(tenant_id, SITE, "lead", [item])
    pipeline.ensure(tenant_id, SITE, item, None)
    return item


def _queued(tenant_id, item_id, body="hello"):
    _deal(tenant_id, item_id)
    msg = outbox.add(tenant_id, SITE, item_id, sequence="presale",
                     step_id="intro", step_label="First reply",
                     body=body, auto=True)
    outbox.set_status(msg["id"], outbox.QUEUED)
    return msg


# ---------------------------------------------------------------------------
# T1 / T2 — the ticket: an outcome the poller never saw is still this run's
# ---------------------------------------------------------------------------


def test_a_delivered_send_is_recorded_sent_even_if_a_scrape_takes_the_slot(
        tenant, monkeypatch):
    """The filed defect, end to end, through the real worker.

    The guest is written to, the worker publishes `done`, a scrape claims and
    finishes on the runner before the poller's first look, and the poller must
    still record this row `sent` — because the outcome is this run's and no
    other run can produce or consume it.
    """
    monkeypatch.setattr(automation, "time", _FastClock())
    notified = []
    monkeypatch.setattr(automation, "_notify_failure",
                        lambda *a, **k: notified.append(a))
    sent_to_guest = []
    _inline_threads(monkeypatch)
    _stub_platform_send(monkeypatch,
                        lambda page, item, text: sent_to_guest.append(item["id"]))
    _scrape_claims_the_slot_when_the_send_returns(monkeypatch, tenant)
    msg = _queued(tenant, "v215-t1")
    _idle()

    automation.send_next(tenant, SITE, timeout=1)

    assert sent_to_guest == ["v215-t1"], (
        "premise: the reply really was delivered — without this the row being "
        "recorded failed would be correct, not a defect")
    snap = runner.get_state(tenant)
    assert snap.get("kind") == "scrape" and snap.get("run_token") is None, (
        "premise: another run owns the slot, so the send's terminal state is "
        "no longer readable there")

    row = outbox.get(msg["id"])
    assert row["status"] == outbox.SENT, (
        f"a delivered send was recorded {row['status']}: {row['error']!r}")
    assert not row["error"]
    assert notified == [], "the operator was told a delivered send had failed"


def test_a_failed_send_is_recorded_with_its_own_error_not_the_timeout(
        tenant, monkeypatch):
    """The mirror of T1, and the reason this is a record and not a shortcut to
    `sent`. The send genuinely fails; the same scrape takes the slot; the row
    must carry *this* run's error, not the timeout text and not another run's
    clean `done`."""
    monkeypatch.setattr(automation, "time", _FastClock())
    notified = []
    monkeypatch.setattr(automation, "_notify_failure",
                        lambda *a, **k: notified.append(a))
    contacted = []
    monkeypatch.setattr(automation, "after_contact",
                        lambda *a, **k: contacted.append(a))

    def explode(page, item, text):
        raise RuntimeError("reply box not found")

    _inline_threads(monkeypatch)
    _stub_platform_send(monkeypatch, explode)
    _scrape_claims_the_slot_when_the_send_returns(monkeypatch, tenant)
    msg = _queued(tenant, "v215-t2")
    _idle()

    automation.send_next(tenant, SITE, timeout=1)

    row = outbox.get(msg["id"])
    assert row["status"] == outbox.FAILED
    assert "reply box not found" in (row["error"] or ""), (
        f"the row must say why THIS send failed, got {row['error']!r}")
    assert "timed out" not in (row["error"] or "")
    assert contacted == [], "a failed send must not advance the follow-up cadence"
    assert len(notified) == 1


# ---------------------------------------------------------------------------
# T3 — VEN-130's guarantee, re-asserted on the new path
# ---------------------------------------------------------------------------


class _ScriptedRunner:
    """Stands in for the runner's two poll seams so the acceptance rule can be
    pinned exactly. `take_send_outcome` is deliberately NOT stubbed: the point
    of this test is what the real record does when the token does not match."""

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
            "updated_at": "2026-09-05T00:00:00"}


def test_another_runs_recorded_outcome_is_never_adopted(tenant, monkeypatch):
    """The no-regression direction, and the one test here that is supposed to
    pass on both heads.

    A durability fix that bought its result by loosening the acceptance rule —
    "if any recorded outcome is lying around, take it" — would satisfy T1 and
    silently reinstate VEN-130's defect. Run A finishes and records a clean
    `done`; run B's poller must not touch it, and B's row must reach the
    timeout instead.
    """
    monkeypatch.setattr(automation, "time", _FastClock())
    monkeypatch.setattr(automation, "_notify_failure", lambda *a, **k: None)
    contacted = []
    monkeypatch.setattr(automation, "after_contact",
                        lambda *a, **k: contacted.append(a))
    stub = _ScriptedRunner({"status": "launching", "running": True, "run_token": "B"},
                           [_terminal("done", None, "Done.")])
    monkeypatch.setattr(runner, "send_reply", stub.send_reply)
    monkeypatch.setattr(runner, "get_state", stub.get_state)
    msg = _queued(tenant, "v215-t3")

    # Run A's success is sitting in the record when run B starts polling.
    runner._publish_terminal("A", tenant, "done", "Reply sent to Dana R.")

    automation.send_next(tenant, SITE, timeout=1)

    row = outbox.get(msg["id"])
    assert row["status"] == outbox.FAILED, (
        "run B adopted run A's recorded outcome")
    assert contacted == [], "the cadence advanced on another run's success"
    assert runner.take_send_outcome("A", tenant) is not None, (
        "run B consumed an outcome that was never its to read")


# ---------------------------------------------------------------------------
# T4 / T5 / T6 / T7 — the record's own contract
# ---------------------------------------------------------------------------


def test_an_outcome_is_readable_once(tenant):
    """Exactly one poller is entitled to an outcome. Leaving it behind would
    also keep a bounded map full of answers nobody is waiting for."""
    runner._publish_terminal("tok-4", tenant, "done", "Reply sent to Dana R.")

    first = runner.take_send_outcome("tok-4", tenant)
    assert first and first["status"] == "done"
    assert first["message"] == "Reply sent to Dana R."
    assert runner.take_send_outcome("tok-4", tenant) is None


def test_an_outcome_is_never_served_to_another_tenant(tenant):
    """`get_state` has a cross-tenant leak guard because a status message can
    name another tenant's traveler — and an outcome carries that message. A read
    path without the same guard makes the invariant one-sided.

    The mismatched read must also not *consume* the entry: silently eating
    another tenant's answer would turn a leak into a lost outcome.
    """
    runner._publish_terminal("tok-5", "1", "done", "Reply sent to Dana R.")

    assert runner.take_send_outcome("tok-5", "2") is None, (
        "another tenant was handed this run's outcome, traveler name and all")
    owner = runner.take_send_outcome("tok-5", "1")
    assert owner and owner["message"] == "Reply sent to Dana R.", (
        "the wrong-tenant read consumed the entry it was refused")


def test_the_outcome_record_is_bounded(tenant):
    """A long-lived dashboard process records an outcome per send forever.
    Entries are consumed on read, but only when someone polls — a browser-server
    caller never does. Cap it, and evict oldest-first."""
    for i in range(runner._OUTCOMES_MAX + 25):
        runner._publish_terminal(f"tok-6-{i}", tenant, "done", f"send {i}")

    assert len(runner._outcomes) == runner._OUTCOMES_MAX, (
        f"the record grew to {len(runner._outcomes)} entries")
    assert runner.take_send_outcome("tok-6-0", tenant) is None, (
        "the oldest entry survived eviction")
    newest = runner._OUTCOMES_MAX + 24
    assert runner.take_send_outcome(f"tok-6-{newest}", tenant) is not None, (
        "eviction dropped the newest entry instead of the oldest")


def test_a_run_whose_slot_write_was_refused_still_owns_its_outcome(
        tenant, monkeypatch):
    """The property that justifies recording unconditionally.

    A worker whose run has been replaced must not write to the slot — that is
    VEN-130's write-side guard, and it stays. But it has still finished, and its
    own poller is still entitled to the answer. Recording only when the slot
    write lands would rebuild a smaller copy of the defect this file is about.

    The slot is stolen from inside the send itself, so the refusal is the real
    `_set_owned` guard refusing a real write.
    """
    sent_to_guest = []

    def send_then_lose_the_slot(page, item, text):
        sent_to_guest.append(item["id"])
        with runner._lock:                      # a newer run claims the runner
            runner._state.update(status="checking", message="Starting…",
                                 running=True, tenant_id=str(tenant),
                                 kind="scrape", run_token=None)

    _inline_threads(monkeypatch)
    _stub_platform_send(monkeypatch, send_then_lose_the_slot)
    _deal(tenant, "v215-t7")
    _idle()

    token = runner.send_reply(tenant, SITE, {"id": "v215-t7", "kind": "lead"},
                              "hi")["run_token"]

    assert sent_to_guest == ["v215-t7"], "premise: the reply was delivered"
    snap = runner.get_state(tenant)
    assert snap.get("running") is True and snap.get("status") != "done", (
        "premise: the terminal slot write really was refused — the stale worker "
        "must not clear a newer run's `running`")
    outcome = runner.take_send_outcome(token, tenant)
    assert outcome is not None and outcome["status"] == "done", (
        "a finished run lost its own outcome because someone else held the slot")


def test_only_terminal_states_are_recorded(tenant, monkeypatch):
    """The record is written at the worker's two terminal sites, not sniffed out
    of `_set_owned`'s kwargs — the progress callback writes through the same
    function with a caller-supplied status, so a predicate there could be fooled
    into publishing "Sending platform reply to Dana R…" as an outcome."""
    _inline_threads(monkeypatch)
    progress = []
    bound = {}

    def send_with_progress(page, item, text):
        # What the browser library's status callback does mid-send: a write
        # through `_set_owned` carrying this run's token and a non-terminal
        # status of the caller's choosing.
        bound["cb"]("checking", "Opening the reply box…")
        progress.append(item["id"])

    monkeypatch.setattr(runner, "_channels", lambda _t: {"platform"})
    monkeypatch.setattr(runner.furnishedfinder, "clear_context", lambda *a, **k: None)
    monkeypatch.setattr(runner.furnishedfinder, "send_reply", send_with_progress)
    monkeypatch.setattr(runner.furnishedfinder, "set_context",
                        lambda _u, _o, cb: bound.update(cb=cb))

    @contextlib.contextmanager
    def fake_page(_tenant_id):
        yield object()

    monkeypatch.setattr(runner.check_leads, "browser_page", fake_page)
    _deal(tenant, "v215-t7b")
    _idle()

    token = runner.send_reply(tenant, SITE, {"id": "v215-t7b", "kind": "lead"},
                              "hi")["run_token"]

    assert progress == ["v215-t7b"], "premise: the progress callback fired"
    outcome = runner.take_send_outcome(token, tenant)
    assert outcome["status"] == "done", (
        "a mid-send progress write was recorded as this run's outcome")
    assert "Opening the reply box" not in outcome["message"]


# ---------------------------------------------------------------------------
# T8 — the token is an in-process value and stays in the process
# ---------------------------------------------------------------------------


LIVE_TOKEN = "v215-live-token-not-for-the-wire"


def _find_run_token(node):
    """Recursively hunt a `run_token` key. A top-level key check would miss the
    browser server, which nests the snapshot under `state`."""
    if isinstance(node, dict):
        if "run_token" in node:
            return True
        return any(_find_run_token(v) for v in node.values())
    if isinstance(node, list):
        return any(_find_run_token(v) for v in node)
    return False


@pytest.fixture()
def in_flight_send(tenant, monkeypatch):
    """A send in flight for this tenant, so every seam below has a live token
    available to leak. Without it these assertions pass over a `None`."""
    with runner._lock:
        runner._state.update(status="checking", message="Sending reply to Dana R.…",
                             running=True, tenant_id=str(tenant), kind="send",
                             run_token=LIVE_TOKEN)
    assert runner.get_state(tenant).get("run_token") == LIVE_TOKEN, (
        "control: the token really is present at the source, so its absence in "
        "the responses below is the strip's doing")
    return tenant


@pytest.fixture()
def client(in_flight_send, monkeypatch):
    import dashboard

    monkeypatch.setattr(automation, "start_drainer", lambda *a, **k: None)
    # Force the in-process branch of `_live_state`: the serverless branch reads
    # from `jobs`, which has no token to leak and would make this vacuous.
    monkeypatch.setattr(dashboard.check_leads, "playwright_available", lambda: True)
    monkeypatch.setattr(dashboard, "_use_worker_queue", lambda: False)
    dashboard.app.config["TESTING"] = True
    dashboard.app.config["WTF_CSRF_ENABLED"] = False
    c = dashboard.app.test_client()
    resp = c.post("/login", data={"email": EMAIL, "password": PASSWORD})
    assert resp.status_code == 302, f"login did not authenticate: {resp.status_code}"
    return c


def test_no_dashboard_response_carries_the_run_token(client, in_flight_send,
                                                     monkeypatch):
    """`/api/status` is polled on a timer and `state | tojson` is inlined into
    the HTML of every page load, so the template is the widest seam of the two
    and the ticket names only the route."""
    # Tenant "1" with no in-process waiter falls back to writing ./OTP_CODE.
    monkeypatch.setattr(runner, "submit_otp", lambda *a: True)
    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.get_json().get("status") == "checking", (
        "control: this response really is the live send's state")
    assert not _find_run_token(status.get_json())
    assert LIVE_TOKEN not in status.get_data(as_text=True)

    page = client.get("/dashboard")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "applyState(" in body, (
        "control: the page really did inline a state object")
    assert LIVE_TOKEN not in body, "every page load published the live token"

    # `/refresh` in-process: a run is already active for this tenant, so
    # `start_scrape` hands back the live `_state` without going through
    # `get_state` at all.
    refresh = client.post("/refresh")
    assert refresh.status_code == 200
    assert not _find_run_token(refresh.get_json())
    assert LIVE_TOKEN not in refresh.get_data(as_text=True)

    otp = client.post("/otp", data={"code": "000000"})
    assert otp.status_code == 200
    assert not _find_run_token(otp.get_json())
    assert LIVE_TOKEN not in otp.get_data(as_text=True)


def _signed(path, body=b"", nonce="v215-1", method="POST"):
    ts = str(int(_real_time.time()))
    digest = hashlib.sha256(body).hexdigest()
    msg = "\n".join([ts, nonce, method, path, digest]).encode()
    # Read the key off the module rather than hardcoding it: whichever test file
    # is imported first in a whole-suite run owns the env.
    sig = hmac.new(browser_server.HMAC_KEY.encode(), msg, hashlib.sha256).hexdigest()
    return {
        "Authorization": f"Bearer {browser_server.AUTH_TOKEN}",
        "X-Shorterm-Timestamp": ts,
        "X-Shorterm-Nonce": nonce,
        "X-Shorterm-Signature": sig,
        "Content-Type": "application/json",
    }


def _bs_post(bs_client, path, obj, nonce):
    body = json.dumps(obj, separators=(",", ":")).encode()
    return bs_client.post(path, data=body, headers=_signed(path, body=body, nonce=nonce))


def test_no_browser_server_response_carries_the_run_token(
        in_flight_send, monkeypatch):
    """All four state-returning `/v1/*` routes, including the reply route, whose
    response is the one that carries a *freshly minted* live token."""
    tenant = in_flight_send
    bs = browser_server.app.test_client()

    state = _bs_post(bs, "/v1/state", {"tenant_id": tenant}, "v215-state")
    assert state.status_code == 200
    assert state.get_json()["state"]["status"] == "checking", (
        "control: this really is the in-flight send's state")
    assert not _find_run_token(state.get_json())
    assert LIVE_TOKEN not in state.get_data(as_text=True)

    monkeypatch.setattr(runner, "submit_otp", lambda *a: True)
    otp = _bs_post(bs, "/v1/otp", {"tenant_id": tenant, "code": "000000"}, "v215-otp")
    assert otp.status_code == 200
    assert not _find_run_token(otp.get_json())
    assert LIVE_TOKEN not in otp.get_data(as_text=True)

    # `/v1/login` while this tenant's run holds the slot: `start_scrape` returns
    # the live `_state` verbatim.
    login = _bs_post(bs, "/v1/login", {"tenant_id": tenant}, "v215-login")
    assert login.status_code == 200
    assert not _find_run_token(login.get_json())
    assert LIVE_TOKEN not in login.get_data(as_text=True)

    # `/v1/reply` on a free runner: this dispatch mints a real token of its own.
    dispatched = []
    monkeypatch.setattr(runner, "_send_worker", lambda *a: dispatched.append(a))
    monkeypatch.setattr(browser_server, "_item_by_id",
                        lambda _t, _i: {"id": "v215-t8", "kind": "lead"})
    _idle()
    reply = _bs_post(bs, "/v1/reply",
                     {"tenant_id": tenant, "item_id": "v215-t8", "text": "hi"},
                     "v215-reply")
    assert reply.status_code == 200
    assert len(dispatched) == 1, "control: the send really was accepted"
    minted = runner.get_state(tenant).get("run_token")
    assert minted, "control: this dispatch really did mint a token"
    assert not _find_run_token(reply.get_json())
    assert minted not in reply.get_data(as_text=True)


def test_every_browser_server_route_is_covered_by_construction(
        in_flight_send, monkeypatch):
    """A hand-picked list of endpoints is a floor, not a list.

    Sweep the url map instead, so a route added later that returns a run state
    is covered without anyone remembering to come back here.
    """
    tenant = in_flight_send
    monkeypatch.setattr(runner, "submit_otp", lambda *a: True)
    monkeypatch.setattr(runner, "_send_worker", lambda *a: None)
    monkeypatch.setattr(browser_server, "_item_by_id",
                        lambda _t, _i: {"id": "v215-t8b", "kind": "lead"})
    monkeypatch.setattr(browser_server.storage, "get_recent",
                        lambda *a, **k: [{"id": "v215-t8b"}])
    monkeypatch.setattr(browser_server.storage, "get_responses", lambda *a, **k: {})
    bs = browser_server.app.test_client()

    paths = sorted({r.rule for r in browser_server.app.url_map.iter_rules()
                    if r.rule.startswith("/v1/") and "POST" in (r.methods or set())})
    assert len(paths) >= 6, f"the sweep found only {paths}"

    for i, path in enumerate(paths):
        res = _bs_post(bs, path, {"tenant_id": tenant, "item_id": "v215-t8b",
                                  "text": "hi", "code": "000000"},
                       f"v215-sweep-{i}")
        assert res.status_code == 200, f"{path} returned {res.status_code}"
        assert not _find_run_token(res.get_json()), f"{path} leaked the token"
        assert LIVE_TOKEN not in res.get_data(as_text=True), f"{path} leaked the token"


# ---------------------------------------------------------------------------
# T9 — the wording that stands between an operator and a duplicate message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "accepted, snapshots, why",
    [
        ({"status": "launching", "running": True, "run_token": "A"},
         [_terminal("launching", "A")],
         "the deadline passed with the worker still going"),
        ({"status": "launching", "running": True},
         [_terminal("done", None, "Done.")],
         "an accepted dispatch that could not be correlated"),
    ],
    ids=["timeout", "no token"],
)
def test_both_may_have_delivered_failures_say_so(tenant, monkeypatch, accepted,
                                                 snapshots, why):
    """Change 1 does not make this redundant. After it, the timeout branch still
    fires for its honest reason — a worker running past the deadline — and that
    worker may be running past it *inside* the reply it already delivered. The
    card renders a primary "Retry send" beside whatever error is stored, so this
    sentence is the only thing standing between the operator and a second copy
    of the same message.
    """
    monkeypatch.setattr(automation, "time", _FastClock())
    notified = []
    monkeypatch.setattr(automation, "_notify_failure",
                        lambda _m, reason: notified.append(reason))
    stub = _ScriptedRunner(accepted, snapshots)
    monkeypatch.setattr(runner, "send_reply", stub.send_reply)
    monkeypatch.setattr(runner, "get_state", stub.get_state)
    msg = _queued(tenant, f"v215-t9-{len(why)}")

    automation.send_next(tenant, SITE, timeout=1)

    # Asserted as a literal, not as `automation.MAY_HAVE_REACHED_GUEST`: against
    # a tree that has no warning at all the constant does not exist either, and
    # this would fail on the missing name rather than on the missing warning.
    warning = "may already have reached the guest; check before retrying"
    row = outbox.get(msg["id"])
    assert row["status"] == outbox.FAILED, why
    assert warning in (row["error"] or ""), (
        f"{why}: the card offers Retry with no warning — {row['error']!r}")
    assert len(notified) == 1
    assert warning in notified[0], (
        f"{why}: the operator notification carries no warning — {notified[0]!r}")
    assert automation.MAY_HAVE_REACHED_GUEST == warning, (
        "both branches must read the sentence from one constant, or they drift")


def test_the_warning_matches_the_wording_outbox_already_uses():
    """One constant, two call sites in `automation`, and the same register
    `outbox` already uses when it abandons a send after its retry budget. Two
    strings saying nearly the same thing drift, and the operator is left
    guessing which failure means "safe to retry"."""
    source = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outbox.py")
    with open(source, encoding="utf-8") as fh:
        outbox_source = fh.read()
    assert automation.MAY_HAVE_REACHED_GUEST in outbox_source, (
        "the abandoned-send wording in outbox.py no longer matches this "
        "constant; the two failure registers have drifted")
