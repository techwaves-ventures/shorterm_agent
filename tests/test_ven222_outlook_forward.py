"""VEN-222: a host forwarding from Outlook opened a second deal.

`_FORWARD_BANNER` knew Gmail's "---------- Forwarded message ----------" and
Apple Mail's "Begin forwarded message:". It did not know Outlook's
"-----Original Message-----", so `_forward_split` returned (0, "") and the
forwarding wrapper was hashed along with the guest's words: the copy got a
different id from the notification it is a copy of, and the host got two deals
for one conversation.

The duplicate deal is the filed symptom. The worse one is that `_guest_text`
also feeds the *stored message body* (`sites/ff_email.py`), so on these
renderings the operator was not shown what the guest wrote — on a forward with
a host's note above the banner the thread showed "FYI — another one." and the
guest's sentence was gone entirely.

Four renderings were broken, not the two that were filed:

    fwd_outlook        Outlook's banner, and `Sent:` rather than `Date:`
    fwd_outlook_intro  the host types a line above that banner
    fwd_outlook_web    Outlook on the web prepends the header block with NO
                       banner at all, so widening the banner never reaches it
    fwd_twice          relayed through two clients; only the outer banner was
                       stripped and the inner one stayed in the fingerprint

## Why the one-line regex fix is wrong, and what these tests protect

Outlook's marker is not a forward banner. Unlike Gmail's and Apple's, Outlook
prints it above quoted history on a *reply* as well — `_QUOTE_START` already
lists it for that reason. Adding it to `_FORWARD_BANNER` fixed all four
renderings and broke a fifth body: a guest reply reading "Sounds good." above
an Outlook quote was stored as the quoted "Any update?" instead. That is the
hazard `_QUOTE_START`'s own comment warns about, and
`test_a_short_reply_above_an_outlook_quote_keeps_the_guests_line` is the
control that keeps it fixed.

The discriminator is the header block: a forward *of a notification* names a
furnishedfinder.com `From:`, a reply quoting our own mail names the host. So
`_FORWARD_BANNER` is byte-identical to before — nothing that worked already can
move — and Outlook's marker is matched separately, behind that gate.

Tenants are namespaced `v222-*` so a whole-suite run cannot collide this file's
dedup rows with a sibling's — `db.DB_PATH` is resolved once per process.
"""
import os
import tempfile

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("FF_CRED_KEY", "c9jwUi0L-fUjf3wjbq74M0lK3ah7fmEfGhjxZ7RehQk=")
os.environ.setdefault("SECRET_KEY", "test-secret")

import storage  # noqa: E402
from sites import ff_email  # noqa: E402

SITE = "furnishedfinder"

WRAPPER = """You have a new message from your traveler.

Property: Sunny 1BR
Tenant: {tenant}
Date received: {received}

{body}
"""

ORIGINAL_DATE = "Wed, 12 Aug 2026 08:00:00 +0000"
FORWARD_DATE = "Sat, 15 Aug 2026 09:12:00 +0000"


def msg(body="Any update?", received="Aug 12, 2026", tenant="Dana R."):
    return WRAPPER.format(tenant=tenant, received=received, body=body)


def _quote(text):
    """A forwarding client indents the body it is quoting one space."""
    return text.replace("\n", "\n ")


# --- how the clients this ticket is about actually render a forward ---------

def fwd_gmail(original):
    return ("---------- Forwarded message ---------\n"
            "From: FurnishedFinder <no-reply@furnishedfinder.com>\n"
            "Date: Sat, Aug 15, 2026 at 9:12 AM\n"
            "Subject: New message\n"
            "To: Host <host@example.com>\n\n") + _quote(original)


def fwd_apple(original):
    return ("Begin forwarded message:\n\n"
            "From: FurnishedFinder <no-reply@furnishedfinder.com>\n"
            "Date: August 15, 2026 at 9:12:00 AM EDT\n"
            "Subject: New message\n"
            "To: Host <host@example.com>\n\n") + _quote(original)


def fwd_outlook(original):
    """Outlook: "-----Original Message-----", and `Sent:` rather than `Date:`."""
    return ("-----Original Message-----\n"
            "From: FurnishedFinder <no-reply@furnishedfinder.com>\n"
            "Sent: Saturday, August 15, 2026 9:12 AM\n"
            "To: Host <host@example.com>\n"
            "Subject: New message\n\n") + _quote(original)


def fwd_outlook_intro(original):
    """The host types a note above the banner before sending it on."""
    return "FYI — another one.\n\n" + fwd_outlook(original)


def fwd_outlook_web(original):
    """Outlook on the web: the header block, and no banner at all."""
    return ("From: FurnishedFinder <no-reply@furnishedfinder.com>\n"
            "Sent: Saturday, August 15, 2026 9:12 AM\n"
            "To: Host <host@example.com>\n"
            "Subject: New message\n\n") + _quote(original)


def fwd_twice(original):
    """Relayed through two clients: Apple inside Gmail."""
    return fwd_apple(fwd_gmail(original))


RENDERINGS = [fwd_outlook, fwd_outlook_intro, fwd_outlook_web, fwd_twice]
RENDERING_IDS = ["outlook", "outlook_with_host_note", "outlook_web", "forwarded_twice"]


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ven222.db")
    return "v222-1"


def ingest(tenant_id, subject, body, received_at):
    """Parse and run the real dedup. True when it was taken as a new message."""
    item = ff_email.parse(subject, body, received_at=received_at)
    assert item is not None, f"the parser refused {subject!r}"
    kind = item.get("kind", "lead")
    return bool(storage.filter_new(tenant_id, SITE, kind, [item])), item


# --- the filed defect, and the two renderings that were not filed -----------

@pytest.mark.parametrize("render", RENDERINGS, ids=RENDERING_IDS)
def test_a_re_forward_does_not_open_a_second_deal(tenant, render):
    """The symptom the host sees: one conversation, one deal.

    Asserted through `parse` + the real `storage.filter_new` rather than by
    comparing hashes, because it is the extra *deal* they experience, not the
    id. All four fail on `ca912ff` and on `63f2df6` before it.
    """
    assert ingest(tenant, "New message", msg(), ORIGINAL_DATE)[0]
    took, _ = ingest(tenant, "Fwd: New message", render(msg()), FORWARD_DATE)
    assert not took, "a re-forward was ingested as a second message"


@pytest.mark.parametrize("render", RENDERINGS, ids=RENDERING_IDS)
def test_the_stored_body_is_the_guests_words_not_the_wrapper(tenant, render):
    """The unfiled, worse symptom: `_guest_text` is the stored message body.

    Before the fix this returned the whole raw email for `fwd_outlook`, the
    host's own note for `fwd_outlook_intro`, and header noise for the other
    two — so the thread view showed the operator anything except what the
    guest actually said.
    """
    _, item = ingest(tenant, "Fwd: New message", render(msg()), FORWARD_DATE)
    assert item["body"].strip() == "Any update?"


@pytest.mark.parametrize("render", RENDERINGS, ids=RENDERING_IDS)
def test_a_forward_arriving_before_the_original_still_collapses(tenant, render):
    """The reversed order, which a host pointing us at a backlog produces."""
    assert ingest(tenant, "Fwd: New message", render(msg()), FORWARD_DATE)[0]
    took, _ = ingest(tenant, "New message", msg(), ORIGINAL_DATE)
    assert not took, "the original arriving after its forward opened a second deal"


# --- the control that makes the FurnishedFinder gate load-bearing -----------

REPLY_SHORT = """Sounds good.

-----Original Message-----
From: Sunny 1BR Host <host@example.com>
Sent: Friday, August 14, 2026 5:00 PM
Subject: Re: your enquiry

 You have a new message from your traveler.
 Property: Sunny 1BR
 Tenant: Dana R.
 Date received: Aug 12, 2026
 Any update?
"""


def test_a_short_reply_above_an_outlook_quote_keeps_the_guests_line(tenant):
    """The regression the naive fix causes, pinned so it cannot come back.

    Here the Outlook marker sits within the same few lines of the top as it
    does on a real forward, and the shapes are otherwise identical. The only
    thing that separates them is the `From:` beneath it: this one is the host,
    so the block is quoted history and the guest's own line is above it. Read
    as a forward banner, everything above the marker is discarded and the
    stored body becomes the quoted "Any update?" — the guest's words replaced
    by our own.
    """
    _, item = ingest(tenant, "New message", REPLY_SHORT, FORWARD_DATE)

    assert item["body"].strip() == "Sounds good."
    assert ff_email._forward_split(REPLY_SHORT) == (0, ""), (
        "a reply quoting the host was read as a forward"
    )


@pytest.mark.parametrize(
    "sender, is_forward",
    [
        ("FurnishedFinder <no-reply@furnishedfinder.com>", True),
        ("FF <no-reply@mail.furnishedfinder.com>", True),
        ("Evil <x@furnishedfinder.com.evil.example>", False),
        ('"no-reply@furnishedfinder.com" <x@evil.example>', False),
        ("Host <host@example.com>", False),
    ],
    ids=["ff", "ff_subdomain", "suffix_lookalike", "display_name_spoof", "host"],
)
def test_the_from_gate_matches_the_domain_the_way_the_allowlist_does(
        sender, is_forward):
    """`parseaddr` and a subdomain test, not a substring and not a raw regex.

    A substring accepts `furnishedfinder.com.evil.example`; a regex over the
    raw line takes the first address-shaped run, which is the display name, and
    accepts `"no-reply@furnishedfinder.com" <x@evil.example>`. That is the
    exact shape `inbound.sender_allowed` already carries scar tissue for, so
    this gate is written the same way.

    Being wrong here cannot forge a deal — the envelope is what authenticates
    mail, in `inbound.sender_allowed`, and this only ever decides how much of a
    body to strip. It is still worth not diverging from the allowlist.
    """
    body = ("-----Original Message-----\n"
            f"From: {sender}\n"
            "Sent: Saturday, August 15, 2026 9:12 AM\n"
            "Subject: New message\n\n") + _quote(msg())

    assert (ff_email._forward_split(body)[0] != 0) is is_forward


# --- FurnishedFinder's own wrapper is not a forward header block ------------

FF_FROM_SENT_SUBJECT = """From: Dana R.
Sent: Aug 12, 2026
Subject: New message about Sunny 1BR

Property: Sunny 1BR
Tenant: Dana R.

Any update?
"""


FF_FROM_DATE_NO_SUBJECT = """From: FurnishedFinder <no-reply@furnishedfinder.com>
Date: Aug 12, 2026

You have a new message from your traveler.

Property: Sunny 1BR
Tenant: Dana R.
Date received: Aug 12, 2026

Any update?
"""


def test_a_headerless_block_without_a_subject_is_not_a_forward(tenant):
    """Why the banner-less branch needs all three labels and not two.

    This is FurnishedFinder's own notification, rendered with its `From:` and
    `Date:` at the top of the text — the same shape as an Outlook-on-the-web
    forward minus the `Subject:`. Requiring only `From:` and a date reads it as
    a forward of itself.

    The damage is not the stored body, which is "Any update?" either way, and
    not the id, which does not move. It is `via_forward`: VEN-155's rule drops
    to the loose key once a message is marked as relayed, so the guest's second
    message of the same day with the same words is silently discarded — the
    exact defect VEN-155 exists to prevent, re-introduced here from the parser
    side. Measured: two deals with the third label, one without it.

    The third label is the conservative direction on purpose. A banner-less
    block is the weakest evidence of a forward there is, and the cost of
    stripping one that is not a forward is higher than the cost of missing one
    that is.
    """
    assert ff_email._forward_split(FF_FROM_DATE_NO_SUBJECT) == (0, "")

    def deliver(stamp):
        return ingest(tenant, "New message", FF_FROM_DATE_NO_SUBJECT, stamp)[0]

    assert deliver(ORIGINAL_DATE)
    assert deliver("Wed, 12 Aug 2026 16:40:00 +0000"), (
        "the guest's second message of the day was read as a re-forward"
    )


def test_ffs_own_wrapper_is_not_read_as_a_headerless_forward(tenant):
    """The cost of recognising Outlook-on-the-web's banner-less forward.

    Once a bare run of headers can mean "forward", FurnishedFinder's own
    rendering — which leads with `From:`/`Sent:`/`Subject:` naming the *guest* —
    looks exactly like one, and stripping it would drop the message. The `From:`
    gate is again what separates them: FF's wrapper names the traveler, not a
    furnishedfinder.com address.
    """
    assert ff_email._forward_split(FF_FROM_SENT_SUBJECT) == (0, "")

    _, item = ingest(tenant, "New message", FF_FROM_SENT_SUBJECT, ORIGINAL_DATE)
    assert "Any update?" in item["body"]


# --- no re-key --------------------------------------------------------------

def test_a_direct_notifications_id_does_not_move(tenant):
    """The hard constraint the ticket set: a live conversation keeps its key.

    Pinned literally. Every id in this file's renderings has to equal the id a
    *direct* notification already produces — that is what makes the forwards
    join the existing deal instead of re-keying it. These two values are
    unchanged from `63f2df6`, measured over a 276-row corpus harvested from
    this suite's own bodies: five bodies moved, all of them currently-broken
    renderings, and every one of them collapsed onto this same pair.
    """
    _, item = ingest(tenant, "New message", msg(), ORIGINAL_DATE)

    assert item["id"] == "016351ae6abe2f12"
    assert item["dedup_id"] == "596d5ef7f49cbe36"


@pytest.mark.parametrize("render", RENDERINGS, ids=RENDERING_IDS)
def test_every_rendering_collapses_onto_the_direct_notifications_key(
        tenant, render):
    """The shape of the re-key, which is the whole safety argument.

    The ids do not scatter — they converge on the key the correctly-parsed
    direct notification already holds. A deal opened by the genuine
    notification keeps its id and the forward joins it.
    """
    _, item = ingest(tenant, "Fwd: New message", render(msg()), FORWARD_DATE)

    assert item["dedup_id"] == "596d5ef7f49cbe36"


# --- VEN-155's guarantee is not traded away ---------------------------------

def test_two_same_day_messages_with_different_words_stay_separate(tenant):
    """Widening what gets stripped must not make two messages look like one."""
    assert ingest(tenant, "New message", msg("Any update?"), ORIGINAL_DATE)[0]
    took, _ = ingest(tenant, "New message", msg("Is parking included?"),
                     "Wed, 12 Aug 2026 16:40:00 +0000")
    assert took


def test_a_guest_who_types_the_banner_text_is_not_truncated(tenant):
    """A guest quoting the marker inside a sentence is still just talking.

    `_OUTLOOK_BANNER` is anchored to a whole line for this reason.
    """
    body = msg(body="My old agent replied under -----Original Message----- "
                    "and I never saw it.")
    _, item = ingest(tenant, "New message", body, ORIGINAL_DATE)

    assert "I never saw it." in item["body"]


# --- the relay depth --------------------------------------------------------

def test_a_third_relay_is_still_stripped(tenant):
    """Three clients deep. Stripping one layer per parse left the inner
    banners in the fingerprint — the same defect one level down."""
    triple = fwd_outlook(fwd_gmail(fwd_apple(msg())))

    _, item = ingest(tenant, "Fwd: New message", triple, FORWARD_DATE)

    assert item["body"].strip() == "Any update?"
    assert item["dedup_id"] == "596d5ef7f49cbe36"
