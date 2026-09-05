"""VEN-219: a swallowed `after_contact` failure strands the deal silently.

`runner._send_worker` records the delivery, then advances the deal's lifecycle
inside a `try/except` that swallows anything the advance raises. The swallow is
deliberate and stays: the guest has already read the reply, so a locked database
must not report it as failed. What was missing is that the advance had *exactly
one* chance, and losing it was invisible — the guest gets a first reply and
never another message, while the board reads "awaiting guest" with nothing
scheduled and, 21 days later, `advance_lifecycle` closes the deal as
"No reply for 21 days".

The fix is three parts, and the middle one is the reason the first exists:

1. `automation.after_contact` is now **one** UPDATE instead of three. Not
   tidying — while it was three, a fault between them stamped the contact
   without advancing the cadence, and that half-applied state is
   indistinguishable from a healthy deal by any predicate derived from the
   columns it writes. Atomicity is what makes `last_contact_at >= sent_at`
   *mean* "the advance for that delivery landed".
2. `automation.reconcile_contacts` derives the owed advances from that
   predicate and pays them, anchored on the **delivery** time rather than the
   repair time, from the worker pass and from every dashboard render.
3. `pipeline.update(uncontacted_since=...)` makes the write a compare-and-set,
   so the reconciler and the send worker racing for the same owed advance
   cannot advance the deal twice.

## What is red on base `0f0100e` — measured, and the two kinds are not the same

Run against a clean `0f0100e`: **12 failed, 4 passed**. That single number is
not the interesting one, because only four of the twelve say anything about
behaviour.

**Behaviour reds (4)** — these call only APIs base already has, so they fail on
base with an `AssertionError` for the filed reason:

* `..._repaired_by_a_render`         -> `assert 'new' == 'contacted'`
* `..._repaired_by_a_worker_pass`    -> `assert 'new' == 'contacted'`
* `..._operator_is_told_...`         -> `expected exactly one alert, got []`
* `..._advance_is_a_single_write`    -> `the advance must be one statement; it
  issued 3`

**Missing-symbol reds (8)** — these exercise `reconcile_contacts`,
`after_contact(at=...)`, or `SETTLE_SECONDS`, which base does not have, so they
fail with `AttributeError`/`TypeError`. That proves nothing about behaviour;
they are here to pin the new code's contract, not to reproduce the defect.

**Green on base by design (4)** — `test_a_successful_send_raises_no_alert` is
the other side of the alert; `test_the_stranded_deal_is_auto_closed_with_a_
reason_that_is_not_true` documents the harm the repair exists to prevent; and
`test_a_closed_deal_is_not_put_back_on_the_follow_up_schedule` (2 cases) pins a
refusal that used to live in `automation.reschedule` and had to be carried into
the merged write by hand. All assert base behaviour that is deliberately
unchanged, so they are guards, not reproductions.

Before this file the suite had no teeth here at all: `grep -rn after_contact
tests/` on base returns one line, inside a docstring. Every test below is new
coverage.
"""
import contextlib
import os
import tempfile
import time
from datetime import date, datetime, timedelta, timezone

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("FF_CRED_KEY", "c9jwUi0L-fUjf3wjbq74M0lK3ah7fmEfGhjxZ7RehQk=")
os.environ.setdefault("SECRET_KEY", "test-secret")

import automation  # noqa: E402
import config  # noqa: E402
import ff_account  # noqa: E402
import pipeline  # noqa: E402
import runner  # noqa: E402
import storage  # noqa: E402
import timeframe  # noqa: E402

SITE = "furnishedfinder"
EMAIL = "host@example.com"
PASSWORD = "a-perfectly-fine-passphrase"

# `presale` step 1 is `followup_1`, anchored on `last_contact_at` at +48h. The
# number is written out here rather than read from `sequences`: an assertion
# that reads its expectation from the same table the code reads agrees with the
# code by construction and can never fail.
FOLLOWUP_1_OFFSET_HOURS = 48

# How far back a test puts a delivery to place it outside the repair's settle
# window. A literal, not `automation.SETTLE_SECONDS + n`: the tests that must
# fail on base for their *behaviour* would otherwise fail on base for a missing
# attribute instead, which proves nothing. `test_a_send_that_is_still_settling_
# ...` asserts the two are consistent, so a raised window cannot quietly turn
# these into no-ops.
PAST_SETTLE_SECONDS = 300


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    """A tenant whose sends can actually complete: connected FF, platform only."""
    import db
    import models

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ven219.db")
    monkeypatch.setattr(pipeline, "_TS_NORMALIZED", False, raising=False)
    monkeypatch.setenv("INSECURE_COOKIES", "1")

    user = models.create_user(EMAIL, PASSWORD)
    tid = str(user.tenant_id)
    config.save_settings(tid, host_name="Test Host", timezone="America/New_York",
                         reply_channels="platform", onboarded="1")
    # Without a connected account the dashboard renders the verification note
    # *instead of* the board, and every assertion about the board passes over a
    # blank page.
    ff_account.connect(tid, "ff@example.com")
    ff_account.mark_state(tid, "connected")
    return tid


@pytest.fixture()
def browser(monkeypatch):
    """Stub the browser seam and nothing else.

    The point of these tests is the real `_send_worker`: a scripted stand-in for
    it would remove the very caller the defect is about. So only Chrome is
    replaced — the status writes, the response row, the swallow and the notify
    are all the shipping code.
    """
    import check_leads
    from sites import furnishedfinder

    @contextlib.contextmanager
    def page(tenant_id):
        yield object()

    monkeypatch.setattr(check_leads, "browser_page", page)
    monkeypatch.setattr(furnishedfinder, "send_reply", lambda page, item, text: None)
    monkeypatch.setattr(furnishedfinder, "send_message_reply",
                        lambda page, item, text: None)
    monkeypatch.setattr(furnishedfinder, "set_context", lambda *a, **k: None)
    monkeypatch.setattr(furnishedfinder, "clear_context", lambda *a, **k: None)


def _seed(tid, item_id, guest):
    item = {"id": item_id, "kind": "lead", "traveler": guest,
            "title": f"2BR Midtown | {guest}", "property_name": "Midtown 2BR"}
    storage.filter_new(tid, SITE, "lead", [item])
    pipeline.ensure(tid, SITE, item, None)
    storage.save_response(tid, SITE, "lead", item_id, status="draft",
                          draft=f"Hi {guest}, happy to hold it.",
                          reason="dates and budget both fit", confidence="high")
    return item


def _settle(tid, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline and runner.get_state(tid).get("running"):
        time.sleep(0.05)
    time.sleep(0.2)


@contextlib.contextmanager
def _advance_raising():
    """Inject a transient fault into the lifecycle advance, and only that.

    Deliberately not `monkeypatch.setattr` + `monkeypatch.undo()`: `undo()`
    reverts *every* patch registered on the fixture's monkeypatch object, so it
    would also put back the real `db.DB_PATH` and the browser seam halfway
    through the test — which silently repoints every later read at an empty
    database and makes the assertions describe a deal that isn't there.
    """
    def boom(*a, **k):
        raise RuntimeError("database is locked")

    real = automation.after_contact
    automation.after_contact = boom
    try:
        yield
    finally:
        automation.after_contact = real


def _send(tid, item_id, guest, *, advance_fails):
    """Deliver a reply through the real `_send_worker`.

    Via `runner.send_reply` rather than `automation.send_next`, deliberately:
    that is the entry point `browser_server.py`'s `/v1/reply` uses, and it
    bypasses `send_next` entirely. A fix placed in `send_next` would pass a
    `send_next`-driven test and still leave this path silent.
    """
    item = _seed(tid, item_id, guest)
    with _advance_raising() if advance_fails else contextlib.nullcontext():
        state = runner.send_reply(tid, SITE, item, f"Hi {guest}, happy to hold it.")
        assert state.get("status") != "busy", (
            "precondition: the send was never dispatched, so nothing below "
            "would be exercising the send worker at all")
        _settle(tid)
    return item


def _sent_at(tid, item_id):
    return (storage.get_responses(tid, SITE).get(item_id) or {}).get("sent_at")


def _backdate_delivery(tid, item_id, **delta):
    """Move the recorded delivery into the past and return the new `sent_at`.

    Most tests need the delivery to sit outside `SETTLE_SECONDS`; the anchor
    test needs it days back, because a same-instant strand cannot tell the
    delivery anchor from the repair anchor apart.
    """
    when = (datetime.now() - timedelta(**delta)).replace(microsecond=0)
    iso = when.isoformat(timespec="seconds")
    storage.update_response(tid, SITE, item_id, sent_at=iso)
    return when, iso


def _assert_stranded(tid, item_id):
    """The precondition every repair test depends on.

    Without this a fix that never strands the deal in the first place would make
    every "and then it was repaired" assertion below pass vacuously.
    """
    deal = pipeline.get(tid, SITE, item_id)
    resp = storage.get_responses(tid, SITE).get(item_id) or {}
    assert resp.get("status") == "sent", "precondition: the reply was not delivered"
    assert deal["stage"] == pipeline.NEW
    assert int(deal["step_index"] or 0) == 0
    assert deal["next_action_at"] is None
    assert deal["last_contact_at"] is None
    return deal


def _assert_advanced(tid, item_id):
    deal = pipeline.get(tid, SITE, item_id)
    assert deal["stage"] == pipeline.CONTACTED
    assert int(deal["step_index"] or 0) == 1
    assert deal["next_action_step"] == "followup_1"
    assert deal["next_action_at"] is not None
    return deal


# ---------------------------------------------------------------------------
# The filed defect, end to end, through each of the two wired-in call sites
# ---------------------------------------------------------------------------

def test_a_delivered_reply_whose_advance_failed_is_repaired_by_a_render(
        tenant, browser, monkeypatch):
    """The defect and its repair on the path most installs actually run.

    The default topology delivers in-process and starts no `worker.py` at all,
    so if the reconciler lived only in the worker the strand would be permanent
    for those installs. This test is what stops the fix from being moved to one
    place: it drives the repair through `GET /dashboard` and nothing else.
    """
    import dashboard

    _send(tenant, "r1", "Iris P.", advance_fails=True)
    deal = _assert_stranded(tenant, "r1")

    # The board is not blank about this deal, it is *plausible*: "we replied,
    # the ball is with them" is a perfectly normal thing for a card to say.
    resp = storage.get_responses(tenant, SITE).get("r1")
    assert pipeline.lead_state(deal, resp) == pipeline.AWAITING_GUEST

    _backdate_delivery(tenant, "r1", seconds=PAST_SETTLE_SECONDS)

    monkeypatch.setattr(automation, "start_drainer", lambda *a, **k: None)
    dashboard.app.config["TESTING"] = True
    dashboard.app.config["WTF_CSRF_ENABLED"] = False
    client = dashboard.app.test_client()
    assert client.post("/login", data={"email": EMAIL, "password": PASSWORD}
                       ).status_code == 302, "login did not authenticate"
    assert client.get("/dashboard").status_code == 200

    _assert_advanced(tenant, "r1")


def test_a_delivered_reply_whose_advance_failed_is_repaired_by_a_worker_pass(
        tenant, browser, monkeypatch):
    """The same repair on the unattended host, where no one loads a page."""
    import digest
    import worker

    _send(tenant, "w1", "Dana G.", advance_fails=True)
    _assert_stranded(tenant, "w1")
    _backdate_delivery(tenant, "w1", seconds=PAST_SETTLE_SECONDS)

    # Two unrelated phases of the pass are stubbed because they drive a real
    # browser and a real mail server. The reconcile under test is not stubbed.
    monkeypatch.setattr(automation, "run_scheduled_checks", lambda *a, **k: None)
    monkeypatch.setattr(digest, "run_due", lambda *a, **k: None)

    worker.run_agent_pass()

    _assert_advanced(tenant, "w1")


def test_the_operator_is_told_when_the_cadence_did_not_start(
        tenant, browser, monkeypatch):
    """The swallow stays, but it stops being silent.

    Asserted by value rather than through a constant the fix introduces: a test
    that reads the sentence from the code it is testing agrees with any sentence
    at all.
    """
    calls = []
    monkeypatch.setattr(runner, "notify",
                        lambda title, body: calls.append((title, body)))

    _send(tenant, "n1", "Iris P.", advance_fails=True)

    assert len(calls) == 1, f"expected exactly one alert, got {calls}"
    title, body = calls[0]
    assert "Follow-up cadence didn't start" == title
    assert "Iris P." in body, "the operator cannot act on an alert with no guest"
    assert "delivered" in body, (
        "the alert must say the reply DID reach the guest — an operator who "
        "reads this as a failed send will send the reply again")


def test_a_successful_send_raises_no_alert(tenant, browser, monkeypatch):
    """The other side of the alert: it must not fire on every healthy send."""
    calls = []
    monkeypatch.setattr(runner, "notify",
                        lambda title, body: calls.append((title, body)))

    _send(tenant, "n2", "Omar K.", advance_fails=False)

    _assert_advanced(tenant, "n2")
    assert calls == []


def test_the_advance_is_a_single_write(tenant, browser, monkeypatch):
    """`after_contact` must be all-or-nothing, or the reconciler cannot see a
    half-applied advance at all: the contact stamp lands, the cadence does not,
    and `last_contact_at >= sent_at` reads exactly like a healthy deal.

    Called with base's three-argument signature on purpose, so this fails on
    base for its behaviour (3 writes) rather than for a missing keyword.
    """
    _seed(tenant, "u1", "Rosa L.")
    real_update = pipeline.update
    calls = []

    def counting(*a, **k):
        calls.append((a, k))
        return real_update(*a, **k)

    # Restored by hand rather than through monkeypatch, which would also undo
    # the fixture's `db.DB_PATH` and leave the assertions reading an empty DB.
    pipeline.update = counting
    try:
        automation.after_contact(tenant, SITE, "u1")
    finally:
        pipeline.update = real_update

    assert len(calls) == 1, (
        f"the advance must be one statement; it issued {len(calls)}")
    _assert_advanced(tenant, "u1")


@pytest.mark.parametrize("stage", [pipeline.LOST, pipeline.COMPLETED])
def test_a_closed_deal_is_not_put_back_on_the_follow_up_schedule(
        tenant, browser, stage):
    """A guard, green on base — and the branch most at risk in this change.

    Folding the three writes into one removed the trip through
    `automation.reschedule`, whose first act was to refuse a `lost` or
    `completed` deal and clear its schedule. That refusal had to be carried over
    by hand into the merged write, and nothing else in the suite covers it: drop
    it and a deal the owner marked lost quietly starts chasing the guest again.
    """
    _seed(tenant, "z1", "Rosa L.")
    pipeline.update(tenant, SITE, "z1", stage=stage)

    automation.after_contact(tenant, SITE, "z1")

    deal = pipeline.get(tenant, SITE, "z1")
    assert deal["stage"] == stage, "the advance must not reopen a closed deal"
    assert deal["next_action_at"] is None, (
        f"a {stage} deal was put back on the follow-up schedule")
    assert deal["next_action_step"] is None


# ---------------------------------------------------------------------------
# The repair's contract
# ---------------------------------------------------------------------------

def test_the_repair_anchors_the_follow_up_on_the_delivery_not_the_repair(
        tenant, browser, monkeypatch):
    """A three-day strand must not push the 48-hour nudge out by three days.

    The literal value is asserted, not `is not None`: the whole difference
    between the two candidate anchors is *which* timestamp, and a same-instant
    strand cannot tell them apart.
    """
    _send(tenant, "a1", "Rosa L.", advance_fails=True)
    _assert_stranded(tenant, "a1")

    # 09:00 local, so the nudge at +48h lands at 09:00 local too and the
    # quiet-hours clamp (08:00-20:00) does not move it.
    delivered = (datetime.now() - timedelta(days=3)).replace(
        hour=9, minute=0, second=0, microsecond=0)
    storage.update_response(tenant, SITE, "a1",
                            sent_at=delivered.isoformat(timespec="seconds"))

    assert automation.reconcile_contacts(tenant, SITE) == 1

    deal = _assert_advanced(tenant, "a1")
    assert deal["last_contact_at"] == delivered.isoformat(timespec="seconds"), (
        "the contact must be stamped at the delivery, not at the repair")

    # Computed here rather than through `sequences`/`timeframe`, so the test
    # owns the expected instant instead of re-deriving it the way the code does.
    expected = (delivered + timedelta(hours=FOLLOWUP_1_OFFSET_HOURS)
                ).astimezone(timezone.utc).replace(tzinfo=None
                                                   ).isoformat(timespec="seconds")
    assert deal["next_action_at"] == expected

    # The contrast that makes the anchor observable: delivered three days ago
    # plus 48h is already overdue. A repair-time anchor would be two days out.
    assert deal["next_action_at"] < timeframe.now(), (
        "a nudge the sequence promised for two days ago must come out overdue, "
        "not rescheduled from the moment the repair happened to run")


def test_two_racing_repairs_advance_the_deal_exactly_once(
        tenant, browser, monkeypatch):
    """The compare-and-set, which is what actually makes the repair safe.

    The reconciler runs on every render and every worker pass, and there is a
    window in which a healthy send has stamped `sent_at` but not yet advanced
    the deal. Without the CAS a render landing in that window advances the deal
    and the send worker then advances it again — VEN-217's double advance, with
    no fault involved at all.
    """
    _send(tenant, "c1", "Priya S.", advance_fails=True)
    _assert_stranded(tenant, "c1")
    _, sent_at = _backdate_delivery(tenant, "c1",
                                    seconds=PAST_SETTLE_SECONDS)

    first = automation.after_contact(tenant, SITE, "c1", at=sent_at,
                                     once_since=sent_at)
    second = automation.after_contact(tenant, SITE, "c1", at=sent_at,
                                      once_since=sent_at)

    assert first is True, "the first caller must be the one that advances"
    assert second is False, "the loser must refuse, not advance the deal again"
    deal = pipeline.get(tenant, SITE, "c1")
    assert int(deal["step_index"] or 0) == 1, (
        "step_index 2 means the guest skips followup_1 entirely")


def test_the_repair_is_idempotent(tenant, browser, monkeypatch):
    """A second pass over an already-repaired deal is a no-op."""
    _send(tenant, "i1", "Sam T.", advance_fails=True)
    _assert_stranded(tenant, "i1")
    _backdate_delivery(tenant, "i1", seconds=PAST_SETTLE_SECONDS)

    assert automation.reconcile_contacts(tenant, SITE) == 1
    before = pipeline.get(tenant, SITE, "i1")

    assert automation.reconcile_contacts(tenant, SITE) == 0
    after = pipeline.get(tenant, SITE, "i1")

    for col in ("stage", "step_index", "next_action_at", "next_action_step",
                "last_contact_at", "first_reply_at"):
        assert before[col] == after[col], f"{col} changed on a no-op pass"


def test_a_healthy_send_is_never_touched(tenant, browser, monkeypatch):
    """With the settle window at zero, so the window is not what is being
    credited. The CAS is; the window only keeps the repair quiet."""
    _send(tenant, "h1", "Omar K.", advance_fails=False)
    before = _assert_advanced(tenant, "h1")

    monkeypatch.setattr(automation, "SETTLE_SECONDS", 0)
    assert automation.reconcile_contacts(tenant, SITE) == 0

    after = pipeline.get(tenant, SITE, "h1")
    for col in ("stage", "step_index", "next_action_at", "next_action_step",
                "last_contact_at", "first_reply_at"):
        assert before[col] == after[col], f"{col} changed on a healthy send"


def test_a_send_that_is_still_settling_is_left_alone_then_repaired(
        tenant, browser, monkeypatch):
    """A delivery inside the settle window is skipped, and picked up after it.

    The second half matters as much as the first: a window that never lets go
    would be a permanently deferred repair rather than a quiet one.
    """
    assert automation.SETTLE_SECONDS < PAST_SETTLE_SECONDS, (
        "the backdate every other test uses no longer clears the settle window, "
        "so those tests are now asserting the window rather than the repair")

    _send(tenant, "s1", "Noor A.", advance_fails=True)
    _assert_stranded(tenant, "s1")

    assert automation.reconcile_contacts(tenant, SITE) == 0, (
        "a send that may still be finishing must not be repaired under it")
    _assert_stranded(tenant, "s1")

    _backdate_delivery(tenant, "s1", seconds=PAST_SETTLE_SECONDS)
    assert automation.reconcile_contacts(tenant, SITE) == 1
    _assert_advanced(tenant, "s1")


def test_a_guest_who_already_replied_is_stamped_but_never_chased(
        tenant, browser, monkeypatch):
    """The hazard in the repair, and the reason it is not a plain retry.

    "Guest replied" is `last_guest_reply_at > last_contact_at`. An unguarded
    repair stamped at the repair time would flip the deal out of "Guest replied"
    and re-arm a follow-up chasing someone for silence they have already broken
    — the exact harm `record_guest_reply` exists to prevent.

    The contact still has to be stamped: it is a fact, and the 21-day abandon
    clock measures from it.
    """
    _send(tenant, "g1", "Priya S.", advance_fails=True)
    _assert_stranded(tenant, "g1")
    _, sent_at = _backdate_delivery(tenant, "g1",
                                    seconds=PAST_SETTLE_SECONDS)
    pipeline.record_guest_reply(tenant, SITE, "g1")

    automation.reconcile_contacts(tenant, SITE)

    deal = pipeline.get(tenant, SITE, "g1")
    assert deal["last_contact_at"] == sent_at, "the contact is a fact; stamp it"
    assert deal["next_action_at"] is None, (
        "re-arming the cadence chases a guest who has already written back")
    assert pipeline.guest_is_waiting(deal), (
        "the badge saying the owner owes this guest a reply must survive the "
        "repair — stamping the contact at the repair time would retire it")


# ---------------------------------------------------------------------------
# The consequence the strand ends in, and that the repair prevents
# ---------------------------------------------------------------------------

def test_the_stranded_deal_is_auto_closed_with_a_reason_that_is_not_true(
        tenant, browser, monkeypatch):
    """A guard, green on base and on the fix — this behaviour is unchanged.

    It is here because it is the cost of the strand, and because the next test
    is only meaningful against it: `_is_abandoned` requires `next_action_at` to
    be *cleared*, which is exactly the stranded state, and measures staleness
    from `max(last_guest_reply_at, last_contact_at, inquiry_at)`. The strand
    leaves `last_contact_at` NULL, so the 21-day clock runs from the inquiry —
    and the guest who was messaged once and never chased is filed as the one
    who went quiet.
    """
    _send(tenant, "x1", "Iris P.", advance_fails=True)
    _assert_stranded(tenant, "x1")

    future = (date.today() + timedelta(days=pipeline.STALE_CLOSE_DAYS + 1)
              ).isoformat()
    moved = pipeline.advance_lifecycle(tenant, SITE, today=future)

    assert moved["lost"] == 1
    deal = pipeline.get(tenant, SITE, "x1")
    assert deal["stage"] == pipeline.LOST
    assert deal["closed_reason"] == "No reply for 21 days", (
        "the operator-facing sentence is the lie, so the test owns it by value")


def test_a_repaired_deal_is_not_auto_closed(tenant, browser, monkeypatch):
    """The same sweep, after the repair: the deal has a scheduled touch again,
    so it is no longer abandoned."""
    _send(tenant, "x2", "Dana G.", advance_fails=True)
    _assert_stranded(tenant, "x2")
    _backdate_delivery(tenant, "x2", seconds=PAST_SETTLE_SECONDS)
    assert automation.reconcile_contacts(tenant, SITE) == 1

    future = (date.today() + timedelta(days=pipeline.STALE_CLOSE_DAYS + 1)
              ).isoformat()
    moved = pipeline.advance_lifecycle(tenant, SITE, today=future)

    assert moved["lost"] == 0
    deal = pipeline.get(tenant, SITE, "x2")
    assert deal["stage"] != pipeline.LOST
    assert deal["closed_reason"] in (None, "")


# ---------------------------------------------------------------------------
# Scope: the repair must not reach across tenants or sites
# ---------------------------------------------------------------------------

def test_the_repair_is_scoped_to_one_tenant(tenant, browser, monkeypatch, tmp_path):
    """Both scoping axes are pinned at the query, per VEN-179 / VEN-181. A
    reconciler that swept every tenant would advance another account's deals
    from whichever dashboard happened to render first."""
    import models

    other = str(models.create_user("other@example.com", PASSWORD).tenant_id)
    config.save_settings(other, host_name="Other Host",
                         timezone="America/New_York", reply_channels="platform",
                         onboarded="1")
    _seed(other, "o1", "Someone Else")
    stamp = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    storage.update_response(other, SITE, "o1", status="sent", sent_at=stamp)

    _send(tenant, "t1", "Iris P.", advance_fails=True)
    _backdate_delivery(tenant, "t1", seconds=PAST_SETTLE_SECONDS)

    assert automation.reconcile_contacts(tenant, SITE) == 1

    intruded = pipeline.get(other, SITE, "o1")
    assert intruded["last_contact_at"] is None, (
        "the other tenant's deal was advanced by this tenant's repair pass")
    assert intruded["stage"] == pipeline.NEW
