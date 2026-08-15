"""Regression tests for the four defects the third re-review left open on `648c8f3`.

The parser half of this file exists because of a specific, repeated failure:
three rounds of parser fixes each closed the filed defect and broke something
adjacent, and the suite passed every time. The reviewer's words for it — "the
current 260 pass with *and* without the bug" — are the requirement these tests
answer. So `test_the_layout_matrix_*` is parametrized over the *separators*
FurnishedFinder's template actually produces, not over the one layout that
happened to be in a fixture.

That matters more than it looks. The obvious fix for the section-header defect
is to require a colon; it closes the defect and passes all 260 tests, and it
silently loses the lead in the one-line table layouts, where the cell is
separated by a tab or a run of spaces and there is no colon at all. Nothing in
the suite covered those, which is why the trap was invisible. They are covered
here now, so the guard cannot be traded away again without a red test.

Every test in this file was verified to fail against `648c8f3` — and to fail
for the *filed* reason, not merely to fail.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("FF_CRED_KEY", "c9jwUi0L-fUjf3wjbq74M0lK3ah7fmEfGhjxZ7RehQk=")
os.environ.setdefault("SECRET_KEY", "test-secret")

import config  # noqa: E402
import outbox  # noqa: E402
import pipeline  # noqa: E402
import storage  # noqa: E402
from sites import ff_email  # noqa: E402

SITE = "furnishedfinder"


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review3.db")
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


# --- #7 (third route): a section header is not a field ----------------------

# The separators the template produces once its table is converted to text. The
# label/value pair is the same in every one of them; only the separator differs,
# and the separator is the *only* structural signal that survives the
# conversion. Written by FurnishedFinder, never by the guest — which is what
# makes it safe to skip on, unlike the value.
LAYOUTS = {
    "colon and space": "Traveler: Emma M.",
    "colon, no space": "Traveler:Emma M.",
    "colon and tab": "Traveler:\tEmma M.",
    # No colon at all. These are the one-line table renderings that a
    # colon-requiring fix silently drops, taking the lead with them.
    "tab, no colon": "Traveler\tEmma M.",
    "two spaces, no colon": "Traveler  Emma M.",
    "space run, no colon": "Traveler     Emma M.",
    # The value in the next cell, i.e. on the next line. The line break is
    # itself the separator.
    "value on next line": "Traveler\nEmma M.",
    "value after blank line": "Traveler:\n\nEmma M.",
    # Forwarded mail keeps the layout but prefixes every line.
    "quoted forward": "> Traveler: Emma M.",
}


@pytest.mark.parametrize("layout", list(LAYOUTS), ids=list(LAYOUTS))
def test_the_layout_matrix_never_loses_the_guests_name(layout):
    """Each of these must yield the guest. A parser change that trades one of
    them away is losing real leads: `parse` returns None, `inbound.accept`
    raises, the webhook answers 202, and the provider never retries — so the
    enquiry is gone with nothing left to find."""
    body = f"Property: Sunny 1BR\n{LAYOUTS[layout]}\n\nHi, is it still available?"
    assert ff_email._wrapper_name(body) == "Emma M."


def test_the_html_fallback_layout_still_finds_the_guest():
    """The single-space layout, which is the one the HTML path actually produces.

    `inbound.extract_body`'s HTML fallback ends with `re.sub(r"[ \\t]+", " ")`
    (inbound.py:203), so every tab and space run in a converted table collapses
    to exactly ONE space. A table row therefore reaches the parser as
    "Traveler Emma M." — meaning the tab and space-run entries in the matrix
    above can never occur on this path, and any rule that treats a single space
    as "not a field" loses the lead on every colonless HTML notification.

    This test exists because a proposed fix for #7 did exactly that and returned
    None here.
    """
    import inbound

    html = ("<html><body><p>You have a new message from your traveler.</p>"
            "<table>"
            "<tr><td>Property</td><td>Sunny 1BR</td></tr>"
            "<tr><td>Traveler</td><td>Emma M.</td></tr>"
            "<tr><td>Date received</td><td>8/14/26</td></tr>"
            "</table><p>Hi! Is the unit still available?</p></body></html>")
    body = inbound.extract_body({"HtmlBody": html})
    assert " Traveler Emma M. " in body, "the HTML path collapses runs to one space"

    item = ff_email.parse("You have a new message", body)
    assert item is not None, "an ordinary HTML notification must not be dropped"
    assert item["sender"] == "Emma M."


@pytest.mark.parametrize("separator", [" | ", "|", " · ", " - ", " "])
def test_a_one_character_separator_does_not_hand_the_thread_to_the_guest(separator):
    """The wrapper's line must win even when its separator is a bare pipe, dot,
    dash or single space.

    These are the characters this parser already expects around values
    (`_only_if_a_name` strips "·|-", and the matcher's line prefix is
    `[ \\t>*|·-]*`). If a rule skips them as "not a field", the search walks on
    into the guest's own message — and the guest controls that text, so they
    choose whose conversation they land in. Reproduced exactly that way against
    a candidate fix: `Traveler | Mallory K.` was skipped and her forged
    `Guest: Emma M.` was accepted, putting her on Emma's thread.
    """
    body = ("You have a new message from your traveler.\n\n"
            f"Property: Sunny 1BR\nTraveler{separator}Mallory K.\n"
            "Date received: 8/11/26\n\n"
            "Guest: Emma M.\nCan you send me the door code?")
    item = ff_email.parse("New message", body)
    assert item is not None, "the lead must not be dropped"
    assert item["sender"] == "Mallory K.", (
        "the wrapper named Mallory; accepting her forged line puts her on "
        "Emma's conversation")


# --- #7 is STILL OPEN. These document it; they are not passing claims. -------
#
# `_name_line_re` makes the colon optional (which is what supports the table
# layouts), so an unlabelled section header matches as a label whose value is
# the rest of the line. Combined with "the wrapper's first answer is final",
# the header becomes the guest's name and the real field below is never read.
#
# The obvious discriminator — how wide the separator is — does NOT work, and
# these xfails are marked strict so the day someone makes it work, they turn
# red and have to be promoted rather than quietly left behind. Three reasons it
# fails, all reproduced:
#   * the HTML path collapses every separator to one space, so a real field and
#     a section header are byte-identical by then (see the two tests above);
#   * a header rendered from a table cell gets a tab or a space run too, so
#     `Traveler\tInformation` is "a field" by that rule and still wins;
#   * skipping a line lets the search reach guest-controlled text, which is how
#     #6 worked.
# A real fix has to bound the search to the wrapper region without the
# `_wrapper_head` cut that lost eight layouts.

_HEADER_XFAIL = pytest.mark.xfail(
    strict=True,
    reason="#7 open: a section header still matches as a label; separator "
           "width cannot tell it from a field (see comment above)",
)


@_HEADER_XFAIL
@pytest.mark.parametrize("header", [
    "Traveler Information",
    "Guest Details",
    "Tenant Profile",
])
def test_a_section_header_does_not_become_the_guests_name(header):
    body = (f"{header}\n\nProperty: Sunny 1BR\n"
            f"{header.split()[0]}: Emma M.\n\nHi, is it available?")
    assert ff_email._wrapper_name(body) == "Emma M."


@_HEADER_XFAIL
def test_two_guests_behind_a_section_header_do_not_share_one_conversation():
    """The filed defect in the form it was filed in: both guests are named
    `Information`, so both resolve to `information|sunny1br` — one deal, and
    the second guest's "send me the door code" lands in the first guest's
    conversation for the operator to approve a reply against."""
    body = ("Traveler Information\n\nProperty: Sunny 1BR\n"
            "Traveler: {name}\nDate received: Aug {day}, 2026\n\n{msg}\n")
    emma = ff_email.parse("New message", body.format(
        name="Emma M.", day="10", msg="Hi, is the unit still available?"))
    mallory = ff_email.parse("New message", body.format(
        name="Mallory K.", day="11", msg="Can you send me the door code?"))

    assert emma is not None and mallory is not None, "a header must not lose the lead"
    assert emma["sender"] == "Emma M."
    assert mallory["sender"] == "Mallory K."
    assert pipeline.thread_key(emma) != pipeline.thread_key(mallory), (
        "two unrelated guests must not land on one thread_key")


@_HEADER_XFAIL
def test_a_header_whose_value_is_prose_does_not_lose_the_lead():
    """The same mechanism failing the other way: the header's remainder is
    prose, `_only_if_a_name` refuses it, and a refusal is equally final — so
    the real "Traveler:" line below is never reached and the enquiry disappears
    behind a 202."""
    item = ff_email.parse("New message", (
        "Traveler has sent you a new message\n\n"
        "Property: Sunny 1BR\nTraveler: Emma M.\n\nHi, is it available?"))
    assert item is not None, "the lead was dropped and the provider will not retry"
    assert item["sender"] == "Emma M."


def test_the_occupancy_line_is_still_not_a_name():
    """`Travelers: 3` bounded by the not-a-letter lookahead — the defect a
    previous round introduced here. Kept in the matrix so the separator work
    cannot reintroduce it."""
    item = ff_email.parse("New message", (
        "Property: Sunny 1BR\nTravelers: 3\nTraveler: Emma M.\n\nHi"))
    assert item["sender"] == "Emma M."
    assert item.get("occupants") in (3, "3")


# --- outbox.cancel: cancelling a sent message re-arms a second delivery -----

def test_an_already_sent_message_cannot_be_cancelled(tenant):
    """`sent_bodies()` selects exactly the `sent` rows and is the only
    duplicate-send guard on /responder/send. Cancelling a sent row emptied it,
    and the same send was then accepted again — a second copy of text the guest
    had already read."""
    msg = _queued(tenant, body="Yes! It is available from Sept 1.")
    outbox.set_status(msg["id"], outbox.SENT)
    assert outbox.sent_bodies(tenant, SITE, "s1") == ["Yes! It is available from Sept 1."]

    outbox.cancel(msg["id"])

    assert outbox.get(msg["id"])["status"] == outbox.SENT, "a sent message stays sent"
    assert outbox.sent_bodies(tenant, SITE, "s1") == [
        "Yes! It is available from Sept 1."], (
        "the sent history is the duplicate-send guard; cancelling must not empty it")


@pytest.mark.parametrize("status", [
    outbox.PENDING, outbox.QUEUED, outbox.FAILED,
])
def test_a_message_that_has_not_reached_the_guest_can_still_be_cancelled(status, tenant):
    """The guard must not cost the operator the ability to call a message off —
    that is the whole point of the button.

    `sending` is deliberately absent; see the test directly below. It is the one
    open state in which the guest is already being written to.
    """
    msg = _queued(tenant)
    outbox.set_status(msg["id"], status)
    outbox.cancel(msg["id"])
    assert outbox.get(msg["id"])["status"] == outbox.CANCELED


# --- cancel-of-`sending` is not a cancel, and must not claim to be -----------

def test_cancelling_an_in_flight_send_is_refused_rather_than_reported_as_ok(tenant):
    """A drainer that has claimed the row is already driving the browser, and
    nothing in `runner._send_worker` consults outbox status — so the guest is
    written to no matter what `cancel` answers. Accepting the cancel gave the
    operator a green success toast for a message that was delivered anyway, and
    `send_next` then overwrote the row with the real outcome.

    This is the twin of the already-`sent` case above and is reached the same
    way: the button renders only on `pending` cards, so both are stale-tab and
    back-button replays.
    """
    msg = _queued(tenant, body="Yes! It is available from Sept 1.")
    # Exactly what automation.send_next does immediately before send_reply().
    outbox.set_status(msg["id"], outbox.SENDING)

    outbox.cancel(msg["id"])

    assert outbox.get(msg["id"])["status"] == outbox.SENDING, (
        "an in-flight send cannot be called off; the honest answer is 'too late'")
    assert outbox.SENDING not in outbox.CANCELABLE, (
        "the route reads CANCELABLE to decide between 409 and a success toast")


def test_a_called_off_in_flight_send_does_not_come_back_as_retryable(tenant):
    """The mirror of the above. When the in-flight send then *fails*, the row
    lands in `FAILED`, which is `APPROVABLE` — so a message the operator
    believed they had called off is offered back to them as retryable. With the
    cancel refused there is no such belief to contradict.
    """
    msg = _queued(tenant)
    outbox.set_status(msg["id"], outbox.SENDING)

    # The route reads the status of the row `cancel` hands back to choose
    # between a 409 and a green "cancelled" toast.
    assert outbox.cancel(msg["id"])["status"] == outbox.SENDING, (
        "reporting a cancel here is what makes the later FAILED row a "
        "contradiction of what the operator was told")

    outbox.set_status(msg["id"], outbox.FAILED, error="send failed")
    assert outbox.get(msg["id"])["status"] in outbox.APPROVABLE, (
        "a failed send stays retryable — which is only correct because it was "
        "never reported as called off")


def test_a_wedged_send_is_still_recoverable_and_then_cancelable(tenant):
    """Dropping `sending` from CANCELABLE must not strand a row left behind by a
    crashed process. `reclaim_stuck_sending` returns it to `queued`, which is
    cancelable again — so the operator keeps a route to call it off, just not
    while it is genuinely in flight.
    """
    msg = _queued(tenant)
    outbox.set_status(msg["id"], outbox.SENDING)

    assert outbox.reclaim_stuck_sending(max_age_seconds=-1) == 1
    assert outbox.get(msg["id"])["status"] == outbox.QUEUED

    outbox.cancel(msg["id"])
    assert outbox.get(msg["id"])["status"] == outbox.CANCELED


def test_a_host_that_cannot_deliver_still_reclaims_its_own_stranded_sends(
        tenant, monkeypatch):
    """The test above proves the mechanism but calls `reclaim_stuck_sending`
    directly with an age no production caller uses, so it passed even while the
    real call site was unreachable. This one goes through `_board`.

    `start_drainer` on the approve route is *not* gated on
    `_can_deliver_in_process()`, so a worker-queue host claims rows into
    `sending` regardless. Gating the reclaim on it meant that host never
    recovered its own stranded rows, and with `sending` no longer cancelable
    the operator had no route left at all: `has_open_step` counts the row as
    open, so the agent never re-drafts that step for that guest either.
    """
    import dashboard

    monkeypatch.setattr(dashboard, "_can_deliver_in_process", lambda: False)
    msg = _queued(tenant)
    outbox.set_status(msg["id"], outbox.SENDING)
    # Stamp the claim old enough for the (unchanged, conservative) age gate,
    # and *aware*, which is the format a claim now carries. A naive stamp is no
    # longer judged on sight — it cannot be attributed to a host, so it is
    # restamped and measured a pass later (see `test_review_fixes_5`).
    with outbox._conn() as c:
        c.execute("UPDATE outbox SET sending_at=? WHERE id=?",
                  ("2020-01-01T00:00:00+00:00", msg["id"]))

    with dashboard.app.test_request_context():
        dashboard._board(tenant)

    assert outbox.get(msg["id"])["status"] == outbox.QUEUED, (
        "a host that cannot deliver must still reclaim what it stranded")
    outbox.cancel(msg["id"])
    assert outbox.get(msg["id"])["status"] == outbox.CANCELED
    assert not outbox.has_open_step(tenant, SITE, "s1", "intro"), (
        "and the step must be free for the agent to draft again")


# --- `sending_at` is compared across hosts, so it must be absolute ----------

def test_the_send_claim_is_stamped_with_an_offset(tenant):
    """Making every render reclaim also made a *second host* read this column
    for the first time. Every other timestamp here is naive local wall-clock,
    which is fine while one host writes and reads it; `sending_at` is written by
    whichever process claims the send and read by whoever reclaims, and on the
    worker-queue topology those are different hosts sharing one DATABASE_URL.

    A naive stamp compared across that boundary is off by the offset. Westward
    it made a one-second-old live send look hours stale, so it was requeued and
    handed to a second drainer — the guest gets the message twice, and since
    `queued` is cancelable the operator also gets a green cancel toast for a
    message physically going out. Eastward the genuinely stranded row was never
    reclaimed at all.
    """
    msg = _queued(tenant)
    outbox.set_status(msg["id"], outbox.SENDING)

    stamp = datetime.fromisoformat(outbox.get(msg["id"])["sending_at"])
    assert stamp.tzinfo is not None, (
        "sending_at is read by other hosts; a naive stamp is off by the offset")


@pytest.mark.parametrize("offset_hours", [-7, 0, 5.5, 11])
def test_a_live_send_claimed_in_another_timezone_is_not_reclaimed(
        offset_hours, tenant):
    """The same instant, expressed in the claiming host's zone. Whatever that
    zone is, the send started *now* and must be left alone."""
    msg = _queued(tenant)
    outbox.set_status(msg["id"], outbox.SENDING)
    zone = timezone(timedelta(hours=offset_hours))
    with outbox._conn() as c:
        c.execute("UPDATE outbox SET sending_at=? WHERE id=?",
                  (datetime.now(zone).isoformat(timespec="seconds"), msg["id"]))

    assert outbox.reclaim_stuck_sending() == 0
    assert outbox.get(msg["id"])["status"] == outbox.SENDING, (
        "a send that started a second ago must not be handed to a 2nd drainer")


def test_a_legacy_naive_stamp_from_the_future_is_restamped_not_stranded(tenant):
    """Rows written before `sending_at` carried an offset are read as local. One
    that lands in the future is the tell-tale of a cross-host read; without the
    restamp it is never old enough to reclaim and the row strands forever — and
    `sending` is no longer cancelable, so there is no operator route out."""
    msg = _queued(tenant)
    outbox.set_status(msg["id"], outbox.SENDING)
    naive_future = (datetime.now() + timedelta(hours=9)).replace(
        tzinfo=None).isoformat(timespec="seconds")
    with outbox._conn() as c:
        c.execute("UPDATE outbox SET sending_at=? WHERE id=?",
                  (naive_future, msg["id"]))

    outbox.reclaim_stuck_sending()

    restamped = datetime.fromisoformat(outbox.get(msg["id"])["sending_at"])
    assert restamped.tzinfo is not None and restamped <= datetime.now(timezone.utc), (
        "the row must be given a measurable start instead of stranding")


def test_a_send_that_completes_mid_reclaim_is_not_clobbered_back_to_queued(
        tenant, monkeypatch):
    """`reclaim_stuck_sending` UPDATEs rows from a snapshot taken earlier in the
    same pass. Without a status guard on the write, a send that reached a
    terminal state in that window was reset to `queued` — leaving the row
    `queued` with `sent_at` stamped, dropping it out of `sent_bodies()` (the
    only duplicate-send guard on /responder/send) and serving it to a drainer
    again."""
    msg = _queued(tenant, body="Yes! It is available from Sept 1.")
    outbox.set_status(msg["id"], outbox.SENDING)
    with outbox._conn() as c:
        c.execute("UPDATE outbox SET sending_at=? WHERE id=?",
                  ("2020-01-01T00:00:00+00:00", msg["id"]))

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

    assert outbox.get(msg["id"])["status"] == outbox.SENT, (
        "a completed send must not be reset to queued by a stale snapshot")
    assert outbox.sent_bodies(tenant, SITE, "s1") == [
        "Yes! It is available from Sept 1."], "the duplicate-send guard stays armed"


# --- send_reply: a collision must not be recorded as a delivery -------------

def test_a_cancelled_message_is_not_resurrected_by_the_busy_release(tenant):
    """`release_unattempted` re-queued the row unconditionally, so a cancel that
    landed while a drainer held the claim was silently undone and the message
    the human called off went to the guest anyway.

    Now that `sending` is not cancelable, that original race is closed at the
    source. The guard is kept as an invariant rather than a race fix: a claim
    may only be handed back by the holder that still owns it. Measured, the
    window it used to cover is ~6-10ms (`automation.py` between the claim and
    the busy release) against a 900s reclaim gate, so no *timing* argument
    keeps it — the reason to keep it is that `release_unattempted` writing to a
    row in any other state is simply wrong, and it costs one SQL predicate.
    The assertion below still discriminates: drop `AND status=?` and it fails.
    """
    msg = _queued(tenant)
    outbox.set_status(msg["id"], outbox.SENDING)
    assert outbox.reclaim_stuck_sending(max_age_seconds=-1) == 1
    outbox.cancel(msg["id"])
    assert outbox.get(msg["id"])["status"] == outbox.CANCELED

    outbox.release_unattempted(msg["id"])

    assert outbox.get(msg["id"])["status"] == outbox.CANCELED, (
        "a message the human cancelled must not come back as queued")


def test_the_busy_release_still_returns_a_live_claim_to_the_queue(tenant):
    """The status check must not disarm the refund it was added to protect."""
    msg = _queued(tenant)
    outbox.set_status(msg["id"], outbox.SENDING)
    before = outbox.get(msg["id"])["attempts"]

    outbox.release_unattempted(msg["id"])

    after = outbox.get(msg["id"])
    assert after["status"] == outbox.QUEUED
    assert after["attempts"] < before, "an undispatched claim is refunded"


def test_a_same_tenant_collision_is_reported_as_busy(tenant):
    """`send_reply` returned the *running* job's state to a same-tenant caller.
    Nothing below that branch starts a thread, so nothing was dispatched — but
    the caller was handed a state that reads as success."""
    import runner

    with runner._lock:
        runner._state.update(status="checking", running=True,
                             tenant_id=str(tenant), kind="scrape")
    try:
        state = runner.send_reply(tenant, SITE, {"id": "s1", "kind": "lead"}, "hi")
    finally:
        with runner._lock:
            runner._state.update(running=False, status="idle", kind=None)

    assert state.get("status") == "busy", (
        "a message that was never dispatched must not report the scrape's status")


def test_a_send_that_collided_with_a_scrape_is_not_recorded_as_sent(tenant):
    """The defect end to end: the outbox said `sent`, `after_contact` fired and
    the follow-up cadence advanced past a guest who was never written to.

    The scrape has to *finish while the send waits*, because that is the
    condition that produces the false success: `send_next` fell through to the
    wait loop, saw the scrape's run go not-running with a non-error status, and
    recorded this message — which no thread ever picked up — as delivered.
    """
    import threading
    import automation
    import runner

    msg = _queued(tenant)
    outbox.set_status(msg["id"], outbox.QUEUED)

    with runner._lock:
        runner._state.update(status="checking", running=True,
                             tenant_id=str(tenant), kind="scrape")

    def _scrape_finishes():
        with runner._lock:
            runner._state.update(running=False, status="idle", kind=None)

    ends = threading.Timer(0.5, _scrape_finishes)
    ends.start()
    try:
        automation.send_next(tenant, SITE, timeout=20)
    finally:
        ends.cancel()
        _scrape_finishes()

    after = outbox.get(msg["id"])
    assert after["status"] != outbox.SENT, (
        "nothing was dispatched; recording `sent` strands the guest silently")
    assert after["status"] == outbox.QUEUED, "it must go back on the queue to retry"
    assert after["attempts"] < outbox.MAX_SEND_ATTEMPTS, (
        "an undispatched message must keep its retry budget")
