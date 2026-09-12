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
from datetime import datetime, timedelta, timezone

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


def _send(tid, item_id, guest, *, advance_fails, guest_replied_at=None):
    """Deliver a reply through the real `_send_worker`.

    Via `runner.send_reply` rather than `automation.send_next`, deliberately:
    that is the entry point `browser_server.py`'s `/v1/reply` uses, and it
    bypasses `send_next` entirely. A fix placed in `send_next` would pass a
    `send_next`-driven test and still leave this path silent.

    `guest_replied_at` stamps a guest message *before* the send, which is the
    ordinary shape of a threaded conversation — the guest writes in, the owner
    answers. Both `runner._scrape_worker` and `inbound` call
    `record_guest_reply` on the parent deal before the owner's reply goes out.
    """
    item = _seed(tid, item_id, guest)
    if guest_replied_at is not None:
        pipeline.record_guest_reply(tid, SITE, item_id, at=guest_replied_at)
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


def test_a_repair_racing_the_send_worker_advances_the_deal_exactly_once(
        tenant, browser, monkeypatch):
    """The same compare-and-set, pinned at the call site instead of the callee.

    The test above proves `after_contact` honours `once_since`. It cannot prove
    `reconcile_contacts` *passes* it: it calls `after_contact` directly, so the
    reconciler's own argument list is one call frame outside everything it can
    see. Drop `once_since=` from the reconciler and the WHERE term stays in
    `pipeline.update`, the callee test stays green, and the whole suite stays
    green — while the interlock the PR rests on is gone.

    So the race is driven through `reconcile_contacts`, and the competitor is
    the send worker's own advance, spelled exactly as `runner._send_worker`
    issues it. The interleaving is the one that hurts: the reconciler decides an
    advance is owed from the deal list, and the worker's advance lands in the
    gap before the reconciler re-reads. The reconciler then reads a deal already
    at step 1 and, unguarded, writes step 2 — the guest skips `followup_1`
    entirely and is chased next with `followup_2`.
    """
    _send(tenant, "w1", "Rafa M.", advance_fails=True)
    _assert_stranded(tenant, "w1")
    _, sent_at = _backdate_delivery(tenant, "w1", seconds=PAST_SETTLE_SECONDS)

    # The seam is `after_contact`'s own re-read, which is the first `pipeline.get`
    # of the pass. Restored before the competitor runs, so the competitor is
    # ordinary shipping code and cannot recurse into this.
    real_get = pipeline.get
    raced = []

    def racing_get(*a, **k):
        if not raced:
            pipeline.get = real_get
            raced.append(automation.after_contact(
                tenant, SITE, "w1", at=sent_at, once_since=sent_at))
        return real_get(*a, **k)

    pipeline.get = racing_get
    try:
        repaired = automation.reconcile_contacts(tenant, SITE)
    finally:
        pipeline.get = real_get

    assert raced == [True], (
        "control: the competing advance never ran, or did not win — nothing "
        "below is about a race")
    deal = pipeline.get(tenant, SITE, "w1")
    assert int(deal["step_index"] or 0) == 1, (
        "step_index 2 means the guest skips followup_1 entirely")
    assert deal["next_action_step"] == "followup_1"
    assert repaired == 0, (
        "the reconciler reported a repair it did not make; the send worker had "
        "already advanced this deal")


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
    credited. The CAS is; the window only keeps the repair quiet.

    The write count is asserted, not just the resulting row. A healthy send
    stamps `sent_at` and `last_contact_at` from the same instant, so the two are
    *equal* — which puts the steady state exactly on the boundary of
    `_advance_owed`'s comparison. Loosen that `>=` to `>` and every healthy deal
    is judged to owe an advance on every render; the CAS then refuses each one,
    so the row is unchanged and an assertion about the row alone still passes.
    What is left is an UPDATE per delivered deal per render, forever, and the
    only way to see it is to count.
    """
    _send(tenant, "h1", "Omar K.", advance_fails=False)
    before = _assert_advanced(tenant, "h1")
    # An old healthy deal: outside the settle window, still with its contact
    # stamped at the delivery. `_send_worker` writes them from one `now`, so
    # backdating both together is the steady state, not a contrivance.
    _, sent_at = _backdate_delivery(tenant, "h1", seconds=PAST_SETTLE_SECONDS)
    pipeline.update(tenant, SITE, "h1", last_contact_at=sent_at)
    before = pipeline.get(tenant, SITE, "h1")

    monkeypatch.setattr(automation, "SETTLE_SECONDS", 0)
    real_update = pipeline.update
    writes = []
    pipeline.update = lambda *a, **k: (writes.append(a), real_update(*a, **k))[1]
    try:
        assert automation.reconcile_contacts(tenant, SITE) == 0
    finally:
        pipeline.update = real_update

    assert writes == [], (
        f"a repair pass over a healthy board must write nothing; it issued "
        f"{len(writes)} deal update(s)")
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


def test_a_guest_who_replied_after_the_delivery_is_advanced_but_not_chased(
        tenant, browser, monkeypatch):
    """The hazard in the repair, and the reason it is not a plain retry.

    "Guest replied" is `last_guest_reply_at > last_contact_at`. An unguarded
    repair stamped at the repair time would flip the deal out of "Guest replied"
    and re-arm a follow-up chasing someone for silence they have already broken
    — the exact harm `record_guest_reply` exists to prevent.

    The contact still has to be stamped: it is a fact, and the 21-day abandon
    clock measures from it. So does the step — `step_index` names the *next*
    step to draft, so a repair that stamps the contact and stops leaves the
    owner's next touch re-sending the reply this guest has already read, and
    leaves it there permanently, since the stamp is what makes `_advance_owed`
    say nothing is owed.

    The state is asserted through `lead_state`, which is what the board actually
    branches on, rather than through the predicate underneath it.
    """
    _send(tenant, "g1", "Priya S.", advance_fails=True)
    _assert_stranded(tenant, "g1")
    _, sent_at = _backdate_delivery(tenant, "g1",
                                    seconds=PAST_SETTLE_SECONDS)
    # After the delivery, which is the ordering the stand-down is for.
    pipeline.record_guest_reply(tenant, SITE, "g1")

    automation.reconcile_contacts(tenant, SITE)

    deal = pipeline.get(tenant, SITE, "g1")
    resp = storage.get_responses(tenant, SITE).get("g1")
    assert deal["last_contact_at"] == sent_at, "the contact is a fact; stamp it"
    assert int(deal["step_index"] or 0) == 1, (
        "the delivered step was not consumed, so the owner's next touch drafts "
        "the message this guest has already read")
    assert deal["next_action_at"] is None, (
        "re-arming the cadence chases a guest who has already written back")
    assert deal["next_action_step"] is None
    assert deal["stage"] == pipeline.CONTACTED, (
        "nurturing means chasing silence, and this guest has broken it")
    assert pipeline.lead_state(deal, resp) == pipeline.GUEST_REPLIED, (
        "the badge saying the owner owes this guest a reply must survive the "
        "repair — stamping the contact at the repair time would retire it")


# ---------------------------------------------------------------------------
# The same guard, on the ordering that carries most of the product's sends
# ---------------------------------------------------------------------------

def test_a_healthy_send_after_an_earlier_guest_message_starts_the_cadence(
        tenant, browser, monkeypatch):
    """Control, and the definition the repair below is measured against.

    A guest writing in *before* the owner's reply is the ordinary threaded
    conversation, not an exception: `record_guest_reply` runs on the parent deal
    for every threaded message (`runner.py`, `inbound.py`), so every send after
    the first on an answered deal has this shape. Nothing about that prior
    message suppresses the cadence — the owner answered them, so the ball is
    back with the guest and `followup_1` is armed as usual.

    Without this control the test below reads as an assertion about repair
    behaviour in isolation. With it, it is an assertion that the repair and the
    healthy send agree, which is the only standard a repair pass can be held to.
    """
    guest_at = (datetime.now() - timedelta(seconds=PAST_SETTLE_SECONDS + 600)
                ).isoformat(timespec="seconds")
    _send(tenant, "k1", "Priya S.", advance_fails=False,
          guest_replied_at=guest_at)

    deal = _assert_advanced(tenant, "k1")
    resp = storage.get_responses(tenant, SITE).get("k1")
    assert pipeline.lead_state(deal, resp) == pipeline.SCHEDULED


def test_a_strand_behind_an_earlier_guest_message_is_still_repaired(
        tenant, browser, monkeypatch):
    """The same ordering with the advance stranded: the repair must land the
    deal where the control above landed it.

    This is the case a guard written as `guest_is_waiting(deal)` gets wrong, and
    it is not a corner. That predicate compares the guest's message to the row's
    own `last_contact_at` — which on a stranded deal is stale by construction,
    because that staleness *is* the fault. So it reports a guest who was
    answered ten minutes later as still waiting, skips the repair, and stamps
    the contact anyway; after that `_advance_owed` sees nothing owed and no
    later pass can reach the deal. The comparison has to be against the delivery
    being repaired.

    Second pass asserted too: "repaired" and "no longer visible to the
    reconciler" look identical from the repaired row alone.
    """
    guest_at = (datetime.now() - timedelta(seconds=PAST_SETTLE_SECONDS + 600)
                ).isoformat(timespec="seconds")
    _send(tenant, "k2", "Priya S.", advance_fails=True,
          guest_replied_at=guest_at)
    stranded = pipeline.get(tenant, SITE, "k2")
    assert (storage.get_responses(tenant, SITE).get("k2") or {}).get(
        "status") == "sent", "precondition: the reply was not delivered"
    assert stranded["last_contact_at"] is None, "precondition: not stranded"
    assert stranded["next_action_at"] is None, "precondition: not stranded"
    _, sent_at = _backdate_delivery(tenant, "k2", seconds=PAST_SETTLE_SECONDS)

    assert automation.reconcile_contacts(tenant, SITE) == 1, (
        "the repair was skipped on the ordering that carries every send after "
        "the first")

    deal = _assert_advanced(tenant, "k2")
    resp = storage.get_responses(tenant, SITE).get("k2")
    assert deal["last_contact_at"] == sent_at
    assert deal["next_action_step"] == "followup_1"
    assert pipeline.lead_state(deal, resp) == pipeline.SCHEDULED, (
        "the control ends `scheduled`; a deal the repair stamped without "
        "advancing reads `awaiting_guest` and is chased by nobody")
    assert automation.reconcile_contacts(tenant, SITE) == 0, (
        "a repaired deal must be settled, not merely unreachable")


def test_a_stood_down_repair_lands_where_the_healthy_send_landed(
        tenant, browser, monkeypatch):
    """The stand-down arm, held to the only standard a repair pass has: the
    row the operation it stands in for would have produced.

    Both arms are the same conversation at the same clock — first reply, guest
    answers, owner replies again, guest answers again — and they differ in one
    thing: on the second arm the lifecycle advance is lost. Run in one tenant so
    a single `reconcile_contacts` pass sees both, which also pins that the pass
    leaves the healthy deal alone.

    The second touch is where the stand-down gets interesting: at `step_index`
    1 a pre-sale advance promotes the deal to `nurturing`. A healthy send does
    promote it — and then the guest's reply pulls it straight back to
    `contacted`, because nurturing means chasing silence. A repair that runs
    after the reply has to arrive at `contacted` directly; promoting on the way
    would leave the two arms describing the same guest differently, and it is
    the repaired one that would be wrong.
    """
    now = datetime.now().replace(microsecond=0)

    def stamp(**delta):
        return (now - timedelta(**delta)).isoformat(timespec="seconds")

    item = {"id": None, "kind": "lead", "traveler": "Priya S.",
            "title": "2BR Midtown | Priya S.", "property_name": "Midtown 2BR"}

    for item_id, strands in (("n1", False), ("n2", True)):
        # Touch one, healthy on both arms, backdated so the second touch is
        # the newer contact.
        _send(tenant, item_id, "Priya S.", advance_fails=False)
        _assert_advanced(tenant, item_id)
        pipeline.update(tenant, SITE, item_id, last_contact_at=stamp(hours=3))
        pipeline.record_guest_reply(tenant, SITE, item_id, at=stamp(hours=2))

        # Touch two: delivered on both arms, advanced on only one.
        with _advance_raising() if strands else contextlib.nullcontext():
            state = runner.send_reply(tenant, SITE, {**item, "id": item_id},
                                      "Hi Priya, those dates work.")
            assert state.get("status") != "busy"
            _settle(tenant)
        storage.update_response(tenant, SITE, item_id, sent_at=stamp(hours=1))
        if not strands:
            # `_send_worker` writes `sent_at` and the contact stamp from one
            # `now`, so they move together — this is the steady state, not a
            # contrivance. On the stranded arm there is no contact stamp to move.
            pipeline.update(tenant, SITE, item_id, last_contact_at=stamp(hours=1))
        # The guest answers our second touch, on both arms.
        pipeline.record_guest_reply(tenant, SITE, item_id, at=stamp(minutes=30))

    healthy_before = pipeline.get(tenant, SITE, "n1")
    assert int(healthy_before["step_index"] or 0) == 2, (
        "precondition: the healthy arm did not take its second step, so there "
        "is nothing meaningful to compare the repair against")
    assert int(pipeline.get(tenant, SITE, "n2")["step_index"] or 0) == 1, (
        "precondition: the second advance was not stranded")

    assert automation.reconcile_contacts(tenant, SITE) == 1, (
        "exactly the stranded arm; a healthy deal must not be repaired")

    healthy = pipeline.get(tenant, SITE, "n1")
    repaired = pipeline.get(tenant, SITE, "n2")
    for col in ("stage", "step_index", "next_action_at", "next_action_step",
                "last_contact_at"):
        assert repaired[col] == healthy[col], (
            f"{col}: repaired {repaired[col]!r} != healthy {healthy[col]!r}")
    assert healthy["stage"] == pipeline.CONTACTED, (
        "guard on the comparison itself: if a guest reply stopped pulling the "
        "deal out of `nurturing`, both arms could agree on the wrong stage")
    for col in ("stage", "step_index", "next_action_at", "next_action_step",
                "last_contact_at"):
        assert healthy[col] == healthy_before[col], (
            f"{col} changed on the healthy arm during the repair pass")


# ---------------------------------------------------------------------------
# The consequence the strand ends in, and that the repair prevents
# ---------------------------------------------------------------------------


def _age_the_inquiry(tid, item_id, *, days):
    """Make the deal genuinely old instead of faking the calendar.

    `advance_lifecycle` now reads TWO dates (VEN-223 split the property frame
    from the server frame, VEN-225 gave `inquiry_at` its own bound), so a test
    that fakes the passage of time through its arguments has to name every
    frame the function grows. Backdating the row's only stamp needs none of
    them: both bounds derive from the real clock, as they do in production.
    Same idiom as `test_agent_lifecycle.py`'s stale-close test.

    Callers pass `STALE_CLOSE_DAYS + 2`, and the `+ 2` is load-bearing, not
    slack. VEN-225's `inquiry_stale_before` is the EARLIER of two
    midnight-anchored bounds, and this tenant is `America/New_York`, so between
    00:00 and ~04:00 UTC the property date is a day behind the server date and
    that bound drops a day with it. A `+ 1` stamp then lands at today's
    time-of-day on the bound's own date — the wrong side of a strict `<` — and
    the test fails for those hours only. Measured both ways; do not shave it.
    """
    old = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with pipeline._conn() as c:
        cur = c.execute(
            "UPDATE deals SET inquiry_at=? WHERE tenant_id=? AND item_id=?",
            (old, tid, item_id))
        assert (cur.rowcount or 0) == 1, (
            "the backdate must land, or the sweep below proves nothing")
    assert pipeline.get(tid, SITE, item_id)["inquiry_at"] == old
    return old


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

    _age_the_inquiry(tenant, "x1", days=pipeline.STALE_CLOSE_DAYS + 2)
    moved = pipeline.advance_lifecycle(tenant, SITE)

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

    _age_the_inquiry(tenant, "x2", days=pipeline.STALE_CLOSE_DAYS + 2)
    moved = pipeline.advance_lifecycle(tenant, SITE)

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
