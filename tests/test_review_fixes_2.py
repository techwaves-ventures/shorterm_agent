"""Regression tests for the ten defects the re-review left open on `32dee81`.

The previous fix pass claimed eight defects closed and had actually closed
four. The reason is worth stating, because these tests are shaped by it: the
fixes were checked against the *new test names* rather than against the
originally filed defect, so a test that fixed a real but different bug made a
live defect look covered, and two defects that had no test at all went
unnoticed.

So every test here is named for the defect it closes, and each one reproduces
the *condition* rather than the symptom:

  * the parser defects need the guest to control part of the text being parsed,
    because that is the whole attack;
  * the timestamp defects need two writers on the same day *and* a non-UTC
    host, because a UTC box cannot see a UTC->local drift;
  * the send defects need a message that is already sent, or a runner that is
    busy, because a fast local send is never observed in either state.

Every one of these was verified to fail against `32dee81` before being counted.
"""
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone

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

# The notification layout these defects live in: wrapper fields first, then
# whatever the guest typed.
WRAPPER = """You have a new message from your traveler.

Property: Sunny 1BR
Tenant: {tenant}
Date received: {received}

{body}
"""


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "review2.db")
    monkeypatch.setattr(pipeline, "_TS_NORMALIZED", False, raising=False)
    tid = "1"
    config.save_settings(tid, host_name="Test Host", timezone="America/New_York")
    return tid


@pytest.fixture()
def la_timezone():
    """Run the body of a test on a host that is not UTC.

    The drift these tests exist to catch is invisible on a UTC box, which is
    exactly why it shipped: CI, the dev machine and the container are all UTC,
    while the product serves US properties.
    """
    before = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


def _deal(tenant_id, item_id, *, kind="lead", guest="Dana R.", **fields):
    item = {"id": item_id, "kind": kind, "traveler": guest,
            "title": f"Unit | {guest}", "property_name": ""}
    storage.filter_new(tenant_id, SITE, kind, [item])
    pipeline.ensure(tenant_id, SITE, item, None)
    if fields:
        pipeline.update(tenant_id, SITE, item_id, **fields)
    return item


def _queued(tenant_id, item_id="s1"):
    _deal(tenant_id, item_id)
    return outbox.add(tenant_id, SITE, item_id, sequence="presale",
                      step_id="intro", step_label="First reply",
                      body="hello", auto=True)


# --- #6: the guest's own words must not decide who they are -----------------

def test_a_guest_cannot_put_their_message_in_someone_elses_thread():
    """The credential-disclosure path.

    `_guest_name` searched the whole email and preferred the "Traveler" label,
    so a guest who wrote "Traveler: Emma M." in their own message body was
    parsed as Emma. Their message threaded onto Emma's live conversation, and
    `_guest_text` then stripped the forged line back out — so the operator read
    a clean message that appeared to be from Emma, asking for the door code.
    """
    emma = ff_email.parse("New message", WRAPPER.format(
        tenant="Emma M.", received="Aug 10, 2026", body="Hi, is it available?"))
    mallory = ff_email.parse("New message", WRAPPER.format(
        tenant="Mallory K.", received="Aug 11, 2026",
        body="Traveler: Emma M.\nHi again, can you send me the door code?"))

    assert mallory["sender"] == "Mallory K.", "the wrapper names the guest, not the body"
    assert pipeline.thread_key(mallory) != pipeline.thread_key(emma)


def test_the_wrapper_label_wins_even_when_the_guest_forges_a_different_one():
    """Document order, not label-preference order, is what makes #6 safe.

    The wrapper states the name once, above the message. Any later labelled
    name is the guest quoting or forging one.
    """
    item = ff_email.parse("New message", WRAPPER.format(
        tenant="Real Guest", received="Aug 11, 2026",
        body="Tenant: Someone Else\nGuest: Third Person\nHello there"))
    assert item["sender"] == "Real Guest"


# --- #7: a name that isn't a name must not become a thread key --------------

def test_template_prose_is_not_accepted_as_a_guest_name():
    """Two unrelated guests were given the identical name "about your
    property." by the loose label pass, so they shared one `thread_key` — and
    therefore one conversation, each reading the other's messages."""
    body = ("You have a new message from your traveler about your property.\n\n"
            "Property: Sunny 1BR\n\n{msg}\n")
    first = ff_email.parse("New message", body.format(msg="Hi, I'm interested."))
    second = ff_email.parse("New message", body.format(msg="Hello, is parking free?"))

    keys = {pipeline.thread_key(i) for i in (first, second) if i}
    assert not any(i and i.get("sender", "").islower() for i in (first, second))
    assert len(keys) != 1 or not keys.pop(), (
        "two unrelated guests must not land on one thread_key")


@pytest.mark.parametrize("junk", [
    "about your property.", ".", "your traveler", "the property",
    "a new message", "is interested in your listing",
])
def test_a_prose_fragment_is_rejected_where_a_name_was_expected(junk):
    assert ff_email._only_if_a_name(junk) == ""


@pytest.mark.parametrize("real", [
    "Emma M.", "Dana R.", "Jean-Luc P.", "Mary Anne S.",
    # The rejection rule has to be permissive, because a rejected name means a
    # dropped notification and the webhook already answered 202 — nobody ever
    # finds out. An earlier version of this guard required every word to be
    # capitalised and capped names at four, which quietly deleted most of these.
    "Maria del Carmen Garcia Lopez", "Juan Carlos de la Cruz Rodriguez",
    "d'Angelo Smith", "李伟", "محمد الفارسي", "さくら 田中",
    "emma m.", "mary smith", "EMMA M.",
    "Emma M. (verified)", "Emma (Travel Nurse)", "Emma M. | RN",
    "Dana R. – Traveler", "Emma & John S.",
    # These were rejected by the first version of this guard: its prose
    # blocklist held "will", "an", "or", "new" and "sent", every one of which
    # is also somebody's given name. Each collision silently cost a real lead.
    "Will Smith", "An Nguyen", "Or Levi", "New Guest", "Sent Kumar",
    "van der Berg", "O'Brien", "José Müller",
])
def test_a_real_name_still_parses(real):
    assert ff_email._only_if_a_name(real) == real


@pytest.mark.parametrize("name", ["Will Smith", "An Nguyen", "Or Levi"])
def test_a_name_that_collides_with_a_function_word_is_not_dropped(name):
    """The unit check above is not enough. Rejection only hurts because `parse`
    then returns None and `inbound.accept` answers 202: the guest's enquiry is
    gone and the provider never retries."""
    item = ff_email.parse("New message", WRAPPER.format(
        tenant=name, received="Aug 15, 2026", body="Is it still available?"))
    assert item is not None, f"{name!r} was rejected and the lead was lost"
    assert item["sender"] == name


def test_an_occupancy_count_is_not_read_as_the_guests_name():
    """`Travelers: 3` must not match the `traveler` label and yield "s: 3".

    The label alternation was unbounded, so it matched the prefix of the longer
    word. Two unrelated guests both came out named "s: 3" — one shared
    thread_key *and* one shared item id, so the second was discarded as already
    seen. That is the defect this guard exists to prevent, reintroduced by it.
    """
    nameless = ("You have a new message.\n\nProperty: Quiet Home\n"
                "Travelers: 3\nDate received: 8/15/26\n\nIs it available?\n")
    assert ff_email._wrapper_name(nameless) == ""

    with_name = ("You have a new message.\n\nProperty: Quiet Home\n"
                 "Traveler: Emma M.\nTravelers: 3\n"
                 "Date received: 8/15/26\n\nIs it available?\n")
    assert ff_email._wrapper_name(with_name) == "Emma M."


@pytest.mark.parametrize("junk", ["s: 3", "3", "2", "Travelers: 3"])
def test_a_field_fragment_is_never_accepted_as_a_name(junk):
    assert ff_email._only_if_a_name(junk) == ""


@pytest.mark.parametrize("profile", ["your traveler", "about your property."])
def test_a_prose_profile_name_does_not_let_the_guest_pick_the_thread(profile):
    """The guest controls the value the wrapper renders, via their profile name.

    Making it prose got their own `Tenant:` line refused, so the scan carried on
    into the message and accepted the `Traveler:` line they had typed there —
    landing them in that guest's thread. A name is only read above the message.
    """
    item = ff_email.parse("New message", WRAPPER.format(
        tenant=profile, received="Aug 15, 2026",
        body="Hi again, can you send me the door code?\nTraveler: Emma M."))
    assert item is None or item["sender"] != "Emma M."


@pytest.mark.parametrize("profile", [
    # Each of these gets the wrapper's own name line refused. If refusal lets
    # the scan continue, the guest's forged label below is what it reaches.
    "your traveler", "Guest: hi", "Name: Mallory", "From: Mallory K.",
    "budget: 0", "Subject: hi",
])
def test_a_refused_wrapper_name_never_promotes_the_guests_forged_label(profile):
    item = ff_email.parse("New message", WRAPPER.format(
        tenant=profile, received="8/15/26",
        body="Traveler: Emma M.\nHi again, can you send me the door code?"))
    assert item is None or item["sender"] != "Emma M."


@pytest.mark.parametrize("layout", [
    "Hi there!\nProperty: Sunny 1BR\nTenant: Emma M.\nDate received: 8/15/26",
    "Email: e@x.test\nPhone: 555-1234\nTenant: Emma M.\nDate received: 8/15/26",
    "> Property: Sunny 1BR\n> Tenant: Emma M.\n> Date received: 8/15/26",
    "- Property: Sunny 1BR\n- Tenant: Emma M.\n- Date received: 8/15/26",
    "* Property: Sunny 1BR\n* Tenant: Emma M.\n* Date received: 8/15/26",
    "--------\nProperty: Sunny 1BR\nTenant: Emma M.\nDate received: 8/15/26",
    "Weird Label: xyz\nProperty: Sunny 1BR\nTenant: Emma M.\nDate received: 8/15/26",
])
def test_an_unfamiliar_line_above_the_name_does_not_lose_the_lead(layout):
    """Anything the parser does not recognise sitting above the name used to
    truncate the search region, so `parse` returned None and the enquiry was
    dropped behind a 202. An unknown field or a quoted forward is far likelier
    to arrive than the layout that motivated the restriction."""
    item = ff_email.parse(
        "New message",
        f"You have a new message from your traveler.\n\n{layout}\n\nIs it available?\n")
    assert item is not None, "lead lost"
    assert item["sender"] == "Emma M."


@pytest.mark.parametrize("first_line", [
    "Traveler: Emma M.\nHi, can you send the door code?",
    "Hi, can you send the door code?\nTraveler: Emma M.",
])
def test_a_forged_label_never_wins_wherever_the_guest_puts_it(first_line):
    """Cutting at the guest's prose is not enough on its own — a forged label
    placed as the *first* body line sits above any prose to cut at."""
    item = ff_email.parse("New message", WRAPPER.format(
        tenant="your traveler", received="8/15/26", body=first_line))
    assert item is None or item["sender"] != "Emma M."


def test_two_guests_with_an_occupancy_line_do_not_share_a_thread():
    """The end-to-end symptom: identical thread_key and identical item id."""
    tmpl = ("You have a new message.\n\nProperty: Quiet Home\n"
            "Traveler: {who}\nTravelers: 3\n"
            "Date received: 8/15/26\n\nIs it available?\n")
    # Both names have to be ones the guard rejected, or the collision doesn't
    # happen: a name that parses cleanly never falls through to `Travelers`.
    a = ff_email.parse("New message", tmpl.format(who="Will Smith"))
    b = ff_email.parse("New message", tmpl.format(who="An Nguyen"))
    assert a and b
    assert a["sender"] != b["sender"]
    assert a["id"] != b["id"], "the second guest was discarded as already seen"
    assert pipeline.thread_key(a) != pipeline.thread_key(b)


def test_a_real_name_survives_the_whole_parse(tenant):
    """The unit above is not enough: rejection is only harmful because `parse`
    turns it into None, and `inbound.accept` turns *that* into a silent 202."""
    item = ff_email.parse("New message", WRAPPER.format(
        tenant="Maria del Carmen Garcia Lopez", received="Aug 12, 2026",
        body="Is it still available?"))
    assert item and item["sender"] == "Maria del Carmen Garcia Lopez"


# --- the forwarder is not the guest -----------------------------------------

FORWARDED = """Begin forwarded message:
From: Pat Landlord
Date: August 12, 2026 at 8:00:00 AM PDT
Subject: New message

You have a new message from your traveler.

Property: Sunny Studio
Traveler: {traveler}
Date received: Aug 12, 2026

Is parking included?
"""


def test_the_host_who_forwarded_the_mail_is_not_the_guest():
    """A forward puts an RFC-822 "From:" above FurnishedFinder's own wrapper.

    Reading names in plain document order let that line win, so every guest a
    host forwarded collapsed onto one thread under the *host's* name — defect
    #7's symptom, reintroduced by the fix for #6. The wrapper's own guest labels
    are therefore always consulted before "From".
    """
    first = ff_email.parse("Fwd: New message", FORWARDED.format(traveler="Dana R."))
    second = ff_email.parse("Fwd: New message", FORWARDED.format(traveler="Emma M."))

    assert (first["sender"], second["sender"]) == ("Dana R.", "Emma M.")
    assert pipeline.thread_key(first) != pipeline.thread_key(second)


# --- the layouts HTML-to-text actually produces ------------------------------

@pytest.mark.parametrize("layout,body", [
    ("value on the same line",
     "You have a new message from your traveler.\n\n"
     "Property: Sunny Studio\nTraveler: Emma M.\nDate received: 8/14/26\n\nIs it available?\n"),
    ("value on the next line",
     "You have a new message from your traveler.\n\n"
     "Property:\nSunny Studio\nTraveler:\nEmma M.\nDate received:\n8/14/26\n\nIs it available?\n"),
    ("table cells, no colon",
     "You have a new message from your traveler.\n\n"
     "Property\nSunny Studio\nTraveler\nEmma M.\nDate received\n8/14/26\n\nIs it available?\n"),
    ("blank line between label and value",
     "You have a new message from your traveler.\n\n"
     "Property:\n\nSunny Studio\nTraveler:\n\nEmma M.\nDate received:\n\n8/14/26\n\nIs it available?\n"),
])
def test_the_label_layouts_html_to_text_produces_all_still_parse(layout, body):
    """The module is deliberately tolerant of this mangling. Requiring "colon,
    same line" silently dropped three of these four — and a dropped
    notification is a lost lead, because the webhook answers 202 either way and
    the provider never retries."""
    item = ff_email.parse("New message", body)
    assert item, f"{layout}: notification was dropped entirely"
    assert item["sender"] == "Emma M."
    assert item["property_name"] == "Sunny Studio"


# --- #8: a message id must separate messages, and only messages -------------

def test_a_guest_repeating_themselves_is_not_discarded_as_already_seen():
    """The original B1 symptom, in the direction that was never fixed.

    A guest bumping a silent thread writes the same words twice ("Any
    update?"). Both hashed to one id, so `filter_new` discarded the second —
    `last_guest_reply_at` never moved and the lifecycle marked them lost for
    not replying while they were actively writing.
    """
    body = WRAPPER.format(tenant="Dana R.", received="", body="Any update?")
    monday = ff_email.parse("New message", body,
                            received_at="Mon, 10 Aug 2026 09:00:00 +0000")
    tuesday = ff_email.parse("New message", body,
                             received_at="Tue, 11 Aug 2026 09:00:00 +0000")
    assert monday["id"] != tuesday["id"]


def test_the_same_message_forwarded_twice_is_still_one_message():
    """The other direction. The id hashed the raw email, which carries the
    relaying wrapper, so one message re-forwarded through a different client
    arrived as a second copy of itself."""
    original = WRAPPER.format(tenant="Dana R.", received="Aug 12, 2026",
                              body="Is parking included?")
    forwarded = ("---------- Forwarded message ----------\n"
                 "From: FurnishedFinder <no-reply@furnishedfinder.com>\n"
                 "Date: Wed, 12 Aug 2026 08:00:00 +0000\n"
                 "Subject: New message\n\n") + original.replace("\n", "\n ")

    assert (ff_email.parse("New message", original)["id"]
            == ff_email.parse("Fwd: New message", forwarded)["id"])


def test_an_empty_template_field_does_not_swallow_the_guests_first_line():
    """Why #8's timestamp fallback was unreachable, and a defect in its own right.

    `_label` let the gap between a label and its value be any whitespace, so a
    field the template left blank ("Date received:" with nothing after it)
    reached past the blank line and took the guest's opening sentence as its
    value. That put message text in `received_at` — and since the received
    stamp is part of a message's id, two different messages that opened with
    the same sentence collided.
    """
    item = ff_email.parse("New message", WRAPPER.format(
        tenant="Dana R.", received="", body="Any update?"))
    assert item.get("received_at", "") != "Any update?"
    # The value-on-the-next-line layout still works when it is genuinely there.
    assert ff_email._label("Date received:\nAug 12, 2026", "Date received") \
        == "Aug 12, 2026"


# --- C1: the same-day follow-up backfill dropped ----------------------------

def test_a_second_reply_on_the_same_day_still_moves_the_deal(tenant):
    """The fix for #1 normalized the column but not the value compared against
    it, so `backfill` — which runs on every dashboard load — silently dropped a
    guest's *follow-up* reply. That is #1's exact symptom, one path over."""
    guest = "Dana R."
    _deal(tenant, "p1", guest=guest)

    def reply(item_id, first_seen):
        item = {"id": item_id, "kind": "message", "sender": guest,
                "title": f"Unit | {guest}", "property_name": "",
                "first_seen": first_seen}   # raw, space-separated: a DB default
        storage.filter_new(tenant, SITE, "message", [item])
        pipeline.backfill(tenant, SITE, {item_id: item}, {})
        return pipeline.get(tenant, SITE, "p1")["last_guest_reply_at"]

    morning = reply("m1", "2026-08-15 08:00:00")
    afternoon = reply("m2", "2026-08-15 14:00:00")
    assert afternoon > morning, "a later reply on the same day must advance the deal"


# --- C2: the two derivations of "state" must agree off UTC too --------------

# The existing drift matrix in test_inbox_filters.py writes only "T"-format
# literals, which is why it could not see this: both sides handled them
# identically. These rows are the shapes the *database* writes.
DB_STAMP_MATRIX = [
    {"stage": pipeline.CONTACTED, "last_contact_at": "2026-08-15T09:00:00",
     "last_guest_reply_at": "2026-08-15 10:00:00"},
    {"stage": pipeline.CONTACTED, "last_contact_at": "2026-08-15 10:00:00",
     "last_guest_reply_at": "2026-08-15T09:00:00"},
    {"stage": pipeline.CONTACTED, "last_contact_at": "2026-08-15 09:00:00",
     "last_guest_reply_at": "2026-08-15 10:00:00"},
    {"stage": pipeline.NEW, "last_guest_reply_at": "2026-08-15 10:00:00"},
]


@pytest.mark.parametrize("fields", DB_STAMP_MATRIX)
def test_sql_and_python_agree_on_database_stamps_off_utc(tenant, la_timezone, fields):
    """`norm_ts` shifted UTC->local, `_state_sql` only swapped the separator, so
    on any non-UTC host the inbox list badged "Guest replied" while the deal's
    own page said the guest was still waiting — about the same deal.

    Both readers now compare in one shape (`cmp_ts`), and the clock conversion
    happens once on write, so they cannot drift.
    """
    import db

    _deal(tenant, "D1")
    # Written straight to the table: this is what a row created before `norm_ts`
    # existed actually looks like.
    with db.connect() as c:
        c.execute(
            "UPDATE deals SET last_contact_at=?, last_guest_reply_at=?, stage=? "
            "WHERE item_id=?",
            (fields.get("last_contact_at"), fields.get("last_guest_reply_at"),
             fields["stage"], "D1"),
        )

    page = pipeline.inbox_page(tenant, SITE)
    assert len(page["rows"]) == 1
    row = page["rows"][0]
    assert row["state"] == pipeline.lead_state(row["deal"], row["response"])


def test_the_sweep_survives_the_first_caller_blowing_up(tenant, la_timezone):
    """The sweep used to run on the caller's connection while latching a module
    flag *before* the UPDATEs. If that caller then raised, `db.Conn.__exit__`
    rolled the conversion back and the latch kept it from ever running again —
    so the rows stayed unconverted and both readers, now agreeing by design,
    agreed on the wrong answer."""
    import db

    _deal(tenant, "R1")
    with db.connect() as c:
        c.execute("UPDATE deals SET last_guest_reply_at=? WHERE item_id=?",
                  ("2026-08-15 10:00:00", "R1"))

    pipeline._TS_NORMALIZED = False
    with pytest.raises(RuntimeError):
        with pipeline._conn():
            raise RuntimeError("the caller blew up mid-transaction")

    with db.connect() as c:
        stored = c.execute(
            "SELECT last_guest_reply_at FROM deals WHERE item_id=?", ("R1",)
        ).fetchone()[0]
    assert stored == "2026-08-15T03:00:00", stored


def test_legacy_utc_rows_are_converted_once_at_rest(tenant, la_timezone):
    """The conversion cannot be written portably in one SQL dialect, so it is
    done to the data instead — which is what keeps the two readers honest."""
    import db

    _deal(tenant, "L1")
    with db.connect() as c:
        c.execute("UPDATE deals SET last_guest_reply_at=? WHERE item_id=?",
                  ("2026-08-15 10:00:00", "L1"))

    pipeline._TS_NORMALIZED = False       # a fresh process
    pipeline.get(tenant, SITE, "L1")      # any query runs the sweep
    stored = pipeline.get(tenant, SITE, "L1")["last_guest_reply_at"]
    assert stored == "2026-08-15T03:00:00", stored


# --- C3: the cached offset lost a second on every stamp ---------------------

def test_a_database_stamp_is_not_shifted_a_second_earlier():
    """`_UTC_OFFSET` was the delta of two `datetime.now()` calls, so it was a
    few microseconds *negative* and `timespec="seconds"` truncated downward.
    Every DB-stamped guest reply moved one second earlier — the wrong direction
    for the comparison it was added to fix."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        assert pipeline.norm_ts("2026-08-15 02:00:00") == "2026-08-15T02:00:00"
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


def test_the_offset_is_not_frozen_across_a_dst_change():
    """It was computed once at import, so a long-running process kept using the
    offset it booted with straight through a DST transition."""
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        winter = pipeline.norm_ts("2026-01-15 12:00:00")   # PST, UTC-8
        summer = pipeline.norm_ts("2026-07-15 12:00:00")   # PDT, UTC-7
        assert winter.endswith("T04:00:00"), winter
        assert summer.endswith("T05:00:00"), summer
    finally:
        os.environ.pop("TZ", None)
        time.tzset()


# --- E: the other Approve & send button -------------------------------------

def test_an_already_sent_message_cannot_be_approved_again(tenant):
    """`/responder/send` was guarded; `/outbox/<id>/approve` — the button the
    dashboard card actually posts to, and the primary approval surface — was
    not. Re-approving a sent message put it back on the queue and it was
    delivered to the guest a second time."""
    msg = _queued(tenant)
    outbox.set_status(msg["id"], outbox.SENT)

    outbox.approve(msg["id"])
    assert outbox.get(msg["id"])["status"] == outbox.SENT


@pytest.mark.parametrize("status", [outbox.SENT, outbox.SENDING,
                                    outbox.QUEUED, outbox.CANCELED])
def test_only_a_pending_or_failed_message_may_be_released(tenant, status):
    msg = _queued(tenant)
    outbox.set_status(msg["id"], status)
    outbox.approve(msg["id"])
    assert outbox.get(msg["id"])["status"] == status


def test_a_failed_send_can_still_be_retried(tenant):
    """The guard must not take away the operator's retry."""
    msg = _queued(tenant)
    outbox.set_status(msg["id"], outbox.FAILED, error="browser died")
    outbox.approve(msg["id"])
    assert outbox.get(msg["id"])["status"] == outbox.QUEUED


def test_the_approve_route_refuses_a_sent_message_over_http(tenant, monkeypatch):
    """The defect was reported over real HTTP, so it is closed over real HTTP."""
    import dashboard
    import models

    monkeypatch.setenv("INSECURE_COOKIES", "1")
    dashboard.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    user = models.create_user("approve@example.com", "a-perfectly-fine-passphrase")
    msg = _queued(str(user.tenant_id))
    outbox.set_status(msg["id"], outbox.SENT)

    client = dashboard.app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(user.id)
        session["_fresh"] = True

    resp = client.post(f"/outbox/{msg['id']}/approve", data={"text": "hi again"})
    assert resp.status_code == 409, resp.data
    assert resp.get_json()["already"] is True
    assert outbox.get(msg["id"])["status"] == outbox.SENT


# --- F: a busy runner must not spend the send budget ------------------------

def test_a_busy_runner_does_not_use_up_the_send_attempts(tenant, monkeypatch):
    """`_drain_loop` retries six times; a scrape holding the browser for ~30s
    therefore burned MAX_SEND_ATTEMPTS with zero deliveries. The message was
    then abandoned on its first genuine stall, telling the operator it "may
    already have reached the guest" — which was false; it had been sent zero
    times."""
    import automation
    import runner

    msg = _queued(tenant)
    outbox.set_status(msg["id"], outbox.QUEUED)
    monkeypatch.setattr(runner, "send_reply", lambda *a, **k: {"status": "busy"})

    for _ in range(6):
        automation.send_next(tenant, SITE)

    after = outbox.get(msg["id"])
    assert after["status"] == outbox.QUEUED
    assert after["attempts"] < outbox.MAX_SEND_ATTEMPTS, (
        "a message that was never dispatched must keep its budget")


def test_a_genuinely_stuck_send_still_uses_one_up(tenant, monkeypatch):
    """The refund must not disarm the cap that bounds a crashed send."""
    msg = _queued(tenant)
    outbox.set_status(msg["id"], outbox.SENDING)
    assert outbox.get(msg["id"])["attempts"] == 1

    # Aware: a naive claim stamp is no longer judged on sight, it is restamped
    # and measured a pass later (see `test_review_fixes_5`).
    long_ago = (datetime.now(timezone.utc)
                - timedelta(hours=10)).isoformat(timespec="seconds")
    with outbox._conn() as c:
        c.execute("UPDATE outbox SET sending_at=? WHERE id=?", (long_ago, msg["id"]))
    assert outbox.reclaim_stuck_sending() == 1
    assert outbox.get(msg["id"])["attempts"] == 1


# --- G: the guard has to be able to see a send ------------------------------

def test_marking_a_reply_sent_works_on_a_deal_that_has_no_response_row(tenant):
    """`update_response` was a bare UPDATE, so it silently did nothing when no
    row existed — which is every deal opened by `pipeline.backfill`. The send
    was recorded nowhere, so the thread page kept offering an enabled "Approve
    & send" over a message the guest had already received, and the 409 behind
    it never fired either. The guard failed *open*."""
    _deal(tenant, "no-resp")
    assert storage.get_responses(tenant, SITE).get("no-resp") is None

    storage.update_response(tenant, SITE, "no-resp", status="sent",
                            draft="already went out",
                            sent_at="2026-08-15T10:00:00")

    stored = storage.get_responses(tenant, SITE)["no-resp"]
    assert stored["status"] == "sent"

    import dashboard
    assert dashboard._sent_state(tenant, "no-resp", stored), (
        "a recorded send must suppress the approve control"
    )


def test_the_upsert_does_not_disturb_an_existing_row(tenant):
    storage.save_response(tenant, SITE, "lead", "has-resp", status="draft",
                          draft="hello", reason="looks good")
    storage.update_response(tenant, SITE, "has-resp", status="sent")

    stored = storage.get_responses(tenant, SITE)["has-resp"]
    assert stored["status"] == "sent"
    assert stored["draft"] == "hello", "unrelated columns must survive"
    assert stored["reason"] == "looks good"


# --- H: the draft has to land where the thread renders it -------------------

def test_a_guest_reply_draft_is_stored_against_the_conversations_deal(
        tenant, monkeypatch):
    """The deal-level duplicate was fixed but the symptom the operator sees was
    not: the draft was saved under the per-message id while the deal and the
    outbox were retargeted to the parent, so `/thread/<parent>` went on
    offering the stale cold intro over the guest's actual question."""
    import responder
    import runner
    import scheduler

    guest = "Dana R."
    _deal(tenant, "L1", guest=guest)
    storage.save_response(tenant, SITE, "lead", "L1", status="sent",
                          draft="Welcome! Here is my intro.")

    monkeypatch.setattr(responder, "load_units", lambda tid: [])
    monkeypatch.setattr(responder, "evaluate_lead", lambda item, tid, units=None: {
        "fit": True, "draft": "Yes, parking is included.", "confidence": "high"})
    monkeypatch.setattr(scheduler, "is_on", lambda tid: False)

    reply = {"id": "M9", "kind": "message", "sender": guest,
             "title": f"Unit | {guest}", "property_name": "",
             "body": "Is parking included?"}
    storage.filter_new(tenant, SITE, "message", [reply])
    runner._draft_new_items(tenant, SITE, "message", [reply])

    responses = storage.get_responses(tenant, SITE)
    assert responses["L1"]["draft"] == "Yes, parking is included.", (
        "the thread page renders the parent's response — the draft belongs there")
    assert "M9" not in responses, "a threaded message has no deal page of its own"


def test_a_failed_redraft_does_not_re_arm_send_over_a_delivered_message(
        tenant, monkeypatch):
    """The retarget in #H gave the draft-error path the parent's response row.

    `save_response` only writes the columns it is handed, so recording the error
    flipped the parent's status `sent -> skipped` while leaving the text that had
    already gone to the guest sitting in `draft`. `_sent_state` keys on that
    status, so an enabled "Approve & send" came back over a message the guest
    had already received — the exact duplicate-delivery defect E and G were
    filed for, reopened through the fix for H.
    """
    import dashboard
    import responder
    import runner
    import scheduler

    guest, sent_text = "Dana R.", "Hi Dana, yes it's free."
    _deal(tenant, "L1", guest=guest)
    storage.save_response(tenant, SITE, "lead", "L1", status="sent", draft=sent_text)
    storage.update_response(tenant, SITE, "L1", sent_at="2026-08-15T10:00:00")
    msg = outbox.add(tenant, SITE, "L1", sequence="presale", step_id="intro",
                     step_label="First reply", body=sent_text, auto=True)
    outbox.set_status(msg["id"], outbox.SENT)

    monkeypatch.setattr(responder, "load_units", lambda tid: [])
    monkeypatch.setattr(responder, "evaluate_lead", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("model unreachable")))
    monkeypatch.setattr(scheduler, "is_on", lambda tid: False)

    reply = {"id": "M1", "kind": "message", "sender": guest,
             "title": f"Unit | {guest}", "property_name": "", "body": "Any update?"}
    storage.filter_new(tenant, SITE, "message", [reply])
    runner._draft_new_items(tenant, SITE, "message", [reply])

    after = storage.get_responses(tenant, SITE)["L1"]
    assert after["draft"] != sent_text, "the delivered text must not remain sendable"
    assert dashboard._sent_state(tenant, "L1", after), (
        "a delivered reply must keep suppressing the send control")


def test_the_delivered_text_itself_can_never_be_re_offered(tenant):
    """`response.status` is one mutable field; the outbox is the durable record
    of what actually reached the guest. Whatever else overwrites the response,
    the exact words already delivered must not come back with a send button."""
    import dashboard

    _deal(tenant, "L2")
    body = "Yes, parking is included."
    msg = outbox.add(tenant, SITE, "L2", sequence="presale", step_id="intro",
                     step_label="First reply", body=body, auto=True)
    outbox.set_status(msg["id"], outbox.SENT)

    # Status says "draft" — the state a failed re-draft or a stale write leaves.
    assert dashboard._sent_state(tenant, "L2", {"status": "draft", "draft": body})
    # A genuinely new draft is still offered, or the thread would dead-end.
    assert dashboard._sent_state(
        tenant, "L2", {"status": "draft", "draft": "Something new entirely."}) == ""


def test_the_thread_reopens_for_a_second_reply_once_the_guest_writes_back(
        tenant, monkeypatch):
    """How `response.status` gets out of `sent` — the reviewer's open question.

    It reopens because the guest replying is what produces a new draft, and
    saving that draft against the conversation's deal (the #H fix) puts the
    status back to `draft`. So the answer is not a separate reset: storing the
    draft in the right place is what reopens the thread, and storing it in the
    wrong place is what dead-ended it.
    """
    import dashboard
    import responder
    import runner
    import scheduler

    guest = "Dana R."
    _deal(tenant, "L1", guest=guest)
    storage.save_response(tenant, SITE, "lead", "L1", status="sent",
                          draft="Cold intro, already sent.")
    before = storage.get_responses(tenant, SITE)["L1"]
    assert dashboard._sent_state(tenant, "L1", before), "precondition: it is closed"

    monkeypatch.setattr(responder, "load_units", lambda tid: [])
    monkeypatch.setattr(responder, "evaluate_lead", lambda item, tid, units=None: {
        "fit": True, "draft": "Yes, parking is included.", "confidence": "high"})
    monkeypatch.setattr(scheduler, "is_on", lambda tid: False)

    reply = {"id": "M9", "kind": "message", "sender": guest,
             "title": f"Unit | {guest}", "property_name": "",
             "body": "Is parking included?"}
    storage.filter_new(tenant, SITE, "message", [reply])
    runner._draft_new_items(tenant, SITE, "message", [reply])

    after = storage.get_responses(tenant, SITE)["L1"]
    assert after["status"] == "draft"
    assert dashboard._sent_state(tenant, "L1", after) == "", (
        "the operator must be able to answer the guest's new question")
