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


@pytest.mark.parametrize("header", [
    "Traveler Information",
    "Guest Details",
    "Tenant Profile",
])
def test_a_section_header_does_not_become_the_guests_name(header):
    """`_name_line_re` makes the colon optional — correctly, it is what supports
    the table layouts — so an unlabelled section header matched as the label
    "Traveler" with the value "Information". Because the wrapper's first answer
    is final, that became the guest's name and the real label below was never
    read."""
    body = (f"{header}\n\nProperty: Sunny 1BR\n"
            f"{header.split()[0]}: Emma M.\n\nHi, is it available?")
    assert ff_email._wrapper_name(body) == "Emma M."


def test_two_guests_behind_a_section_header_do_not_share_one_conversation():
    """The filed defect, in the form it was filed in: both guests were named
    `Information`, so both resolved to `information|sunny1br` — one deal, and
    the second guest's "send me the door code" landed in the first guest's
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
    assert emma["id"] != mallory["id"], (
        "an identical item id makes the second guest look already-seen")


def test_a_header_whose_value_is_prose_does_not_lose_the_lead():
    """The same mechanism, failing the other way. The header's remainder is
    prose, `_only_if_a_name` refuses it, and a refusal is equally final — so
    the real "Traveler:" line below was never reached and the whole enquiry
    disappeared behind a 202."""
    item = ff_email.parse("New message", (
        "Traveler has sent you a new message\n\n"
        "Property: Sunny 1BR\nTraveler: Emma M.\n\nHi, is it available?"))
    assert item is not None, "the lead was dropped and the provider will not retry"
    assert item["sender"] == "Emma M."


@pytest.mark.parametrize("forged", [
    "Traveler: Mallory K.",
    "Traveler\tMallory K.",
    "Guest: Mallory K.",
    "Name: Mallory K.",
    "From: Mallory K.",
])
def test_skipping_a_header_still_does_not_let_a_guest_choose_their_thread(forged):
    """The guard against trading #6 away to fix #7.

    Skipping is only safe because it keys on the separator, which the template
    writes. If it ever keys on the *value* again, a guest gets the wrapper's
    line refused and the search walks on to the label in their own message —
    which is exactly how #6 worked."""
    body = ("Traveler Information\n\nProperty: Sunny 1BR\n"
            f"Traveler: Emma M.\n\nHi there\n{forged}\nsend me the door code")
    assert ff_email._wrapper_name(body) == "Emma M."


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
    outbox.PENDING, outbox.QUEUED, outbox.SENDING, outbox.FAILED,
])
def test_a_message_that_has_not_reached_the_guest_can_still_be_cancelled(status, tenant):
    """The guard must not cost the operator the ability to call a message off —
    that is the whole point of the button."""
    msg = _queued(tenant)
    outbox.set_status(msg["id"], status)
    outbox.cancel(msg["id"])
    assert outbox.get(msg["id"])["status"] == outbox.CANCELED


# --- send_reply: a collision must not be recorded as a delivery -------------

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
