"""Regression tests for the eight defects found reviewing this branch.

Every one of these defects shipped past a green 150-test suite and a manual
browser pass. That is the fact worth designing against: the seed data happened
to dodge each of them. So these tests are written to reproduce the *condition*
that hid the bug, not just the symptom —

  * the timestamp bugs need two rows written by *different* writers on the
    *same day*, because the demo data was seeded by one writer across days;
  * the threading bugs need a guest reply that actually threads, because a
    threaded reply is the case that has no deal row of its own;
  * the send bugs need a message in flight, because a fast local send is never
    observed mid-flight.

Each test names the customer-visible failure it prevents.
"""
import os
import tempfile
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("FF_CRED_KEY", "c9jwUi0L-fUjf3wjbq74M0lK3ah7fmEfGhjxZ7RehQk=")
os.environ.setdefault("SECRET_KEY", "test-secret")

import config  # noqa: E402
import inbound  # noqa: E402
import outbox  # noqa: E402
import pipeline  # noqa: E402
import storage  # noqa: E402
from sites import ff_email  # noqa: E402

SITE = "furnishedfinder"


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review.db")
    tid = "1"
    config.save_settings(tid, host_name="Test Host", timezone="America/New_York")
    return tid


def _deal(tenant_id, item_id, *, kind="lead", guest="Dana R.", **fields):
    item = {"id": item_id, "kind": kind, "traveler": guest,
            "title": f"Unit | {guest}", "property_name": ""}
    storage.filter_new(tenant_id, SITE, kind, [item])
    pipeline.ensure(tenant_id, SITE, item, None)
    if fields:
        pipeline.update(tenant_id, SITE, item_id, **fields)
    return item


# --- D1: the two clocks -----------------------------------------------------

def test_a_guest_reply_stamped_by_the_database_still_counts_as_a_reply(tenant):
    """The defect that hid the whole "Guest replied" tab.

    `last_guest_reply_at` is copied from `seen.first_seen`, which the database
    fills with CURRENT_TIMESTAMP — "2026-08-15 10:00:00", space-separated and
    UTC. `last_contact_at` is written by the app as "2026-08-15T09:00:00".
    Compared as strings, the space sorts below "T", so a guest who replied an
    hour *after* our message compared as having replied before it, and the deal
    never reached the tab the operator lives in.
    """
    _deal(tenant, "d1")
    pipeline.update(tenant, SITE, "d1",
                    last_contact_at="2026-08-15T09:00:00",
                    last_guest_reply_at="2026-08-15 10:00:00")  # DB-style stamp

    deal = pipeline.get(tenant, SITE, "d1")
    assert pipeline.lead_state(deal, {"status": "sent"}) == pipeline.GUEST_REPLIED

    # And the SQL definition has to agree, or the tab shows a different set.
    page = pipeline.inbox_page(tenant, SITE, state=pipeline.GUEST_REPLIED)
    assert [r["deal"]["item_id"] for r in page["rows"]] == ["d1"]


def test_the_raw_comparison_that_used_to_invert(tenant):
    """The ordering itself, stated plainly: a space-separated stamp one hour
    later must not read as earlier than a T-separated one."""
    later_utc = "2026-08-15 10:00:00"
    earlier_app = "2026-08-15T09:00:00"
    assert later_utc < earlier_app, "precondition: raw strings do invert"
    assert pipeline.norm_ts(later_utc) > pipeline.norm_ts(earlier_app)


# --- D2: the conversations that had no row ----------------------------------

def test_messages_filter_shows_conversations_not_just_orphans(tenant):
    """"Show me all the messages" showed almost none of them.

    A guest reply that threads onto an existing conversation deliberately gets
    no deal row of its own — that is what stops every reply opening a duplicate
    deal. But the Messages filter matched on the deal's *origin* kind, so the
    only rows it could ever return were the messages that failed to thread.
    """
    _deal(tenant, "lead-with-reply", kind="lead",
          last_guest_reply_at="2026-08-15T10:00:00")
    _deal(tenant, "quiet-lead", kind="lead")
    _deal(tenant, "orphan-msg", kind="message")

    ids = {r["deal"]["item_id"]
           for r in pipeline.inbox_page(tenant, SITE, kind="message")["rows"]}
    assert "lead-with-reply" in ids, "a lead the guest replied to IS a message thread"
    assert "orphan-msg" in ids
    assert "quiet-lead" not in ids


# --- D3: the send that repeated forever -------------------------------------

def _queued(tenant_id, item_id="s1"):
    return outbox.add(tenant_id, SITE, item_id, sequence=pipeline.PRESALE
                      if hasattr(pipeline, "PRESALE") else "presale",
                      step_id="intro", step_label="First reply",
                      body="hello", auto=True)


def test_a_send_deferred_by_quiet_hours_is_not_reclaimed_the_moment_it_starts(tenant):
    """The duplicate-message defect, and the reason it was unbounded.

    Reclaim measured age from `approved_at` — stamped when the message was
    approved, which the quiet-hours clamp can put hours before delivery. So a
    message approved at 22:00 and sent at 08:00 was already "stuck" by every
    definition the moment a drainer picked it up, and every dashboard load
    requeued it. It had no attempt cap, so it kept going.
    """
    msg = _queued(tenant)
    long_ago = (datetime.now() - timedelta(hours=10)).isoformat(timespec="seconds")
    with outbox._conn() as c:
        c.execute("UPDATE outbox SET approved_at=? WHERE id=?", (long_ago, msg["id"]))

    outbox.set_status(msg["id"], outbox.SENDING)  # a drainer picks it up now
    assert outbox.reclaim_stuck_sending() == 0, "an in-flight send must be left alone"
    assert outbox.get(msg["id"])["status"] == outbox.SENDING


def test_a_genuinely_crashed_send_is_still_recovered(tenant):
    """The reclaim must keep doing its job — this is not a disable."""
    msg = _queued(tenant)
    outbox.set_status(msg["id"], outbox.SENDING)
    stale = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    with outbox._conn() as c:
        c.execute("UPDATE outbox SET sending_at=? WHERE id=?", (stale, msg["id"]))

    assert outbox.reclaim_stuck_sending() == 1
    assert outbox.get(msg["id"])["status"] == outbox.QUEUED


def test_a_send_that_keeps_stalling_gives_up_instead_of_repeating(tenant):
    """Each reclaim is a message we cannot prove didn't arrive. Bound the loop."""
    msg = _queued(tenant)
    stale = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    for _ in range(outbox.MAX_SEND_ATTEMPTS):
        outbox.set_status(msg["id"], outbox.SENDING)
        with outbox._conn() as c:
            c.execute("UPDATE outbox SET sending_at=? WHERE id=?", (stale, msg["id"]))
        outbox.reclaim_stuck_sending()

    outbox.set_status(msg["id"], outbox.SENDING)
    with outbox._conn() as c:
        c.execute("UPDATE outbox SET sending_at=? WHERE id=?", (stale, msg["id"]))
    outbox.reclaim_stuck_sending()

    final = outbox.get(msg["id"])
    assert final["status"] == outbox.FAILED
    assert "attempts" in (final["error"] or "").lower()


def test_a_send_with_no_start_stamp_is_not_treated_as_infinitely_old(tenant):
    """An unreadable stamp used to mean `started = 0`, which is older than any
    cutoff — so the failure mode of the clock was a duplicate message."""
    msg = _queued(tenant)
    with outbox._conn() as c:
        c.execute("UPDATE outbox SET status=?, sending_at=NULL WHERE id=?",
                  (outbox.SENDING, msg["id"]))
    assert outbox.reclaim_stuck_sending() == 0
    assert outbox.get(msg["id"])["sending_at"], "it should stamp one to measure from"


# --- D4: the duplicate deal and the cold intro ------------------------------

def test_a_scraped_guest_reply_joins_the_conversation_instead_of_restarting_it(
        tenant, monkeypatch):
    """The scrape path was missing the threading branch that `inbound.store` and
    `pipeline.backfill` both have.

    Consequences, all customer-visible: a second deal for a guest already in
    conversation; that duplicate shadowing the real one in `find_thread` (which
    orders newest-id-first); the original's nurture clock still running at
    someone who had just written; and an autopilot message labelled "First
    reply" — introducing the property — sent into the middle of a negotiation.
    """
    import runner

    original = _deal(tenant, "lead-1", kind="lead", guest="Emma M.")
    parent = pipeline.get(tenant, SITE, "lead-1")
    assert parent["thread_key"]

    sent = {}

    def fake_evaluate(item, tid, units=None):
        return {"fit": True, "draft": "Sure — it is available.", "unit_id": None,
                "reason": "good fit", "confidence": "high", "tenant_email": ""}

    def fake_enqueue(tid, site, item_id, body, reason="", step=None):
        sent.update(item_id=item_id, step=step)
        return {"status": "pending_approval"}

    import automation
    import responder
    import scheduler

    monkeypatch.setattr(responder, "evaluate_lead", fake_evaluate)
    monkeypatch.setattr(responder, "load_units", lambda tid: [])
    monkeypatch.setattr(scheduler, "is_on", lambda tid: True)
    monkeypatch.setattr(automation, "enqueue_autopilot_reply", fake_enqueue)

    reply = {"id": "msg-1", "kind": "message", "traveler": "Emma M.",
             "title": original["title"], "property_name": ""}
    runner._draft_new_items(tenant, SITE, "message", [reply])

    assert pipeline.get(tenant, SITE, "msg-1") is None, "the reply must not open a deal"
    refreshed = pipeline.get(tenant, SITE, "lead-1")
    assert refreshed["last_guest_reply_at"], "the real deal should be stamped"
    assert not refreshed["next_action_at"], "their reply stands the chase down"

    assert sent["item_id"] == "lead-1", "queue against the deal, not the message"
    assert sent["step"] and sent["step"]["id"] == "guest_reply", \
        "a reply mid-conversation is not the introduction"


# --- D5: the send button over an already-sent message -----------------------

def test_an_already_sent_reply_offers_no_second_send(tenant):
    """A delivered reply keeps its text in `response.draft` — the sender writes
    the sent body back over it — so "has a draft" never meant "unsent". The
    thread view showed an enabled Approve & send above a message that had
    already gone, under the words "Nothing is sent until you approve it"."""
    import dashboard

    _deal(tenant, "sent-1")
    assert dashboard._sent_state(tenant, "sent-1", {"status": "draft"}) == ""

    blocked = dashboard._sent_state(
        tenant, "sent-1", {"status": "sent", "sent_at": "2026-08-15T10:00:00"})
    assert blocked and "sent" in blocked.lower()


def test_a_send_still_in_flight_offers_no_second_send(tenant):
    """The other half: while a send is queued or mid-flight the button was still
    live, so a second click put a duplicate in front of the guest."""
    import dashboard

    _deal(tenant, "flight-1")
    outbox.add(tenant, SITE, "flight-1", sequence="presale", step_id="intro",
               step_label="First reply", body="hello", auto=True)
    blocked = dashboard._sent_state(tenant, "flight-1", {"status": "draft"})
    assert blocked, "a queued send must suppress the approve control"


# --- D6: the sender allowlist -----------------------------------------------

def test_a_furnishedfinder_address_in_the_display_name_does_not_authorize():
    """The allowlist took the first address-shaped text in the header, and the
    display name comes first — so this was accepted as FurnishedFinder."""
    assert not inbound.sender_allowed(
        '"no-reply@furnishedfinder.com" <attacker@evil.com>')


def test_real_furnishedfinder_senders_still_pass():
    for good in ("no-reply@furnishedfinder.com",
                 "FurnishedFinder <no-reply@furnishedfinder.com>",
                 "x@mail.furnishedfinder.com"):
        assert inbound.sender_allowed(good), good
    for bad in ("someone@gmail.com", "a@evil-furnishedfinder.com", "", "junk"):
        assert not inbound.sender_allowed(bad), bad


def test_reply_to_alone_cannot_satisfy_the_allowlist():
    """Reply-To says where a reply should go, not where mail came from. An
    attacker sets it freely, and it used to outrank the actual From."""
    assert inbound.extract_sender({"reply_to": "no-reply@furnishedfinder.com"}) == ""
    assert inbound.extract_sender(
        {"headers": {"X-Forwarded-For": "no-reply@furnishedfinder.com"}}) == ""


# --- D7 / D8: what the guest actually wrote ---------------------------------

def test_the_thread_does_not_quote_our_own_message_back_as_the_guests():
    """A reply from a mail client carries the whole prior thread beneath it —
    including our last message, which was then displayed as the guest's words."""
    body = ("You have a new message from your traveler.\n"
            "Traveler: Emma M.\n\n"
            "Hi! Is it still available for September?\n\n"
            "On Jul 18, 2026, Host wrote:\n"
            "> Hello Emma, the unit is available.\n"
            "> Let me know.\n"
            "--\nSent from my iPhone")
    assert ff_email._guest_text(body) == "Hi! Is it still available for September?"


def test_the_guests_own_words_are_not_deleted_for_starting_with_a_field_name():
    """The wrapper filter dropped any line beginning with a template label,
    anywhere in the mail. Guests answer using exactly those words, so the single
    most important sentence in the reply was removed before anyone saw it."""
    body = ("You have a new message from your traveler.\n"
            "Traveler: Emma M.\n\n"
            "Budget: I can do $2000.\n"
            "Pets: yes, one cat\n")
    text = ff_email._guest_text(body)
    assert "I can do $2000." in text
    assert "yes, one cat" in text
    assert "Emma M." not in text, "the actual wrapper must still be stripped"


def test_wrapper_fields_are_still_stripped():
    """The fix must not become "show the whole notification again"."""
    body = ("You have a new tenant lead.\n"
            "Property: Maple Suite\n"
            "Traveler: Emma M.\n"
            "Budget: $2,400/month\n"
            "Date received: July 19, 2026\n"
            "Traveling with pets: Yes\n\n"
            "Looking forward to hearing from you.\n")
    assert ff_email._guest_text(body) == "Looking forward to hearing from you."
