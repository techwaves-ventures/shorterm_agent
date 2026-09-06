"""VEN-155: two guest messages sent on one day collapsed onto one deal id.

FurnishedFinder's own `Date received:` line is day-precision in every rendering
this repo has ever seen — "July 19, 2026", "8/15/26", never a time — and it
outranked the transport `Date` in the message id. `_body_fingerprint` strips
quoted history, so a guest bumping a silent thread with the same words ("Any
update?") was separated from their first send by that stamp alone. At day
precision the two were indistinguishable: `storage.filter_new` dropped the
second before anything was written, the board never showed it, and the nurture
sequence kept chasing someone who had written twice.

The reason it took a second key to fix is that the two requirements pull in
opposite directions, and neither stamp satisfies both:

    the same message arriving twice  must collapse -> needs a stamp that
                                                      survives a relay
    a guest writing the same words   must separate -> needs a stamp with
      twice on one day                               better than day precision

FF's line is stable but coarse; the transport `Date` is precise but rewritten
by every relay. Ranking the transport stamp first — the fix the ticket
suggested evaluating — only trades this defect for its mirror image, so the
tests below assert *both* directions on every path. Every test here was checked
to fail (or, where noted, to pass for the wrong reason) on `63f2df6`.

Tenants are namespaced `v155-*` so a whole-suite run cannot collide this file's
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

# The real FurnishedFinder message template: a day-precision received line and
# then whatever the guest typed.
WRAPPER = """You have a new message from your traveler.

Property: Sunny 1BR
Tenant: {tenant}
Date received: {received}

{body}
"""

ORIGINAL_DATE = "Wed, 12 Aug 2026 08:00:00 +0000"
# Same calendar day as ORIGINAL_DATE, eight hours later. The day-precision line
# cannot tell these apart; the transport Date can.
SAME_DAY_LATER = "Wed, 12 Aug 2026 16:40:00 +0000"
# A forward is a new email, so it carries its own, later transport Date.
FORWARD_DATE = "Sat, 15 Aug 2026 09:12:00 +0000"


def msg(body="Any update?", received="Aug 12, 2026", tenant="Dana R."):
    return WRAPPER.format(tenant=tenant, received=received, body=body)


def _quote(text):
    """A forwarding client indents the body it is quoting one space."""
    return text.replace("\n", "\n ")


# --- how mail clients actually render a forward header block ----------------
# Only the first of these is valid RFC 5322. The suite used to test that one
# alone, which is why a fix that relied on parsing the forwarded `Date:` looked
# workable: it is the single rendering where that date parses.

def fwd_rfc5322(original):
    return ("---------- Forwarded message ----------\n"
            "From: FurnishedFinder <no-reply@furnishedfinder.com>\n"
            "Date: Wed, 12 Aug 2026 08:00:00 +0000\n"
            "Subject: New message\n\n") + _quote(original)


def fwd_gmail(original):
    """Gmail: three trailing dashes, and a `Date` that is not RFC 5322."""
    return ("---------- Forwarded message ---------\n"
            "From: FurnishedFinder <no-reply@furnishedfinder.com>\n"
            "Date: Sat, Aug 15, 2026 at 9:12 AM\n"
            "Subject: New message\n"
            "To: Host <host@example.com>\n\n") + _quote(original)


def fwd_apple(original):
    """Apple Mail: a "Begin forwarded message:" banner, prose date."""
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


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ven155.db")
    return "v155-1"


def ingest(tenant_id, subject, body, received_at):
    """Parse and run the real dedup. True when it was taken as a new message."""
    item = ff_email.parse(subject, body, received_at=received_at)
    assert item is not None, f"the parser refused {subject!r}"
    kind = item.get("kind", "lead")
    return bool(storage.filter_new(tenant_id, SITE, kind, [item])), item


# --- the filed defect -------------------------------------------------------

def test_two_same_day_messages_with_the_same_words_are_two_messages(tenant):
    """The filed defect, through the real ingest path rather than the hash.

    Asserting the two ids differ is not enough: an id can differ and still be
    dropped, and it is the drop the guest experiences.
    """
    took_first, first = ingest(tenant, "New message", msg(), ORIGINAL_DATE)
    took_second, second = ingest(tenant, "New message", msg(), SAME_DAY_LATER)

    assert took_first
    assert took_second, (
        "the guest's second message of the day was discarded by the dedup"
    )
    assert first["id"] != second["id"]


def test_a_message_redelivered_unchanged_still_collapses(tenant):
    """The guard on the fix above: the provider retrying one delivery, byte for
    byte, must stay one message. Its transport `Date` is the sender's and does
    not change on redelivery, so the strict key catches it."""
    assert ingest(tenant, "New message", msg(), ORIGINAL_DATE)[0]
    took_again, _ = ingest(tenant, "New message", msg(), ORIGINAL_DATE)
    assert not took_again


def test_two_same_day_messages_with_different_words_stay_separate(tenant):
    """The other guard: this must keep working, and must not be the *reason*
    the test above passes."""
    assert ingest(tenant, "New message", msg("Any update?"), ORIGINAL_DATE)[0]
    took, _ = ingest(tenant, "New message",
                     msg("Is parking included?"), SAME_DAY_LATER)
    assert took


# --- the direction a stricter stamp would have broken -----------------------

@pytest.mark.parametrize("render", [fwd_rfc5322, fwd_gmail, fwd_apple],
                         ids=["rfc5322", "gmail", "apple_mail"])
def test_a_re_forward_does_not_open_a_second_deal(tenant, render):
    """A host forwarding a notification we already hold must not create a
    second conversation — including when their client's forward header is not
    RFC 5322, which is every client but one.

    This is the direction that fails if the transport `Date` simply replaces
    FF's line in the id: the forward carries a *new* transport date, so a
    strict key alone reads it as a new message.
    """
    assert ingest(tenant, "New message", msg(), ORIGINAL_DATE)[0]
    took, _ = ingest(tenant, "Fwd: New message", render(msg()), FORWARD_DATE)
    assert not took, "a re-forward was ingested as a second message"


def test_a_forward_arriving_before_the_original_still_collapses(tenant):
    """The reversed arrival order, which the rule has to handle symmetrically.

    A host pointing us at their mailbox often forwards a backlog first, so the
    forward lands before the live feed delivers the original. Reading the
    forward banner on the *incoming* mail alone would accept the late original
    as a second message; the bit is recorded on the stored row so the later
    direct delivery recognises it too.
    """
    assert ingest(tenant, "Fwd: New message", fwd_gmail(msg()), FORWARD_DATE)[0]
    took, _ = ingest(tenant, "New message", msg(), ORIGINAL_DATE)
    assert not took, "the original arriving after its forward opened a second deal"


@pytest.mark.xfail(
    strict=True,
    reason="Pre-existing and out of VEN-155's scope: `_FORWARD_BANNER` does not "
           "know Outlook's '-----Original Message-----', so the banner is never "
           "stripped and the body fingerprints differ. Fails identically on "
           "63f2df6 — measured, not assumed. Strict, so fixing it turns this "
           "red and forces the marker off.",
)
def test_an_outlook_re_forward_does_not_open_a_second_deal(tenant):
    """Documented, not fixed. Widening the banner regex changes what
    `_strip_forwarded` removes, which changes every message fingerprint and
    therefore re-keys live conversations — the one thing this ticket was told
    not to do. It needs its own ticket and its own re-key measurement."""
    assert ingest(tenant, "New message", msg(), ORIGINAL_DATE)[0]
    took, _ = ingest(tenant, "Fwd: New message", fwd_outlook(msg()), FORWARD_DATE)
    assert not took


# --- the rows that already exist -------------------------------------------

def test_a_row_written_before_the_second_key_still_dedups(tenant):
    """The transitional guarantee, and the reason the *loose* key is the one
    left unchanged.

    Rows written by the previous version hold the loose key in `item_id` and
    have no `alt_id`. Their strict key was never recorded, so a strict miss
    against such a row proves nothing — the only safe reading is the one the
    table had when the row was written. Getting this wrong duplicates every
    open conversation on the deploy.
    """
    item = ff_email.parse("New message", msg(), received_at=ORIGINAL_DATE)
    legacy_id = item["dedup_id"]
    assert legacy_id != item["id"], "the two keys must actually differ here"

    # Exactly what the previous version wrote: item_id = the loose key, and
    # neither of the new columns set.
    with storage._conn() as c:
        c.execute(
            "INSERT INTO seen (tenant_id, site, kind, item_id, payload) "
            "VALUES (?,?,?,?,?)",
            (tenant, SITE, "message", legacy_id, "{}"),
        )

    took, _ = ingest(tenant, "New message", msg(), ORIGINAL_DATE)
    assert not took, "a redelivery re-ingested against a pre-upgrade row"


def test_an_existing_database_gains_the_columns_without_losing_its_rows(tmp_path,
                                                                       monkeypatch):
    """The migration, on a database that already has rows.

    Every test above builds its schema from `CREATE TABLE`, so none of them
    reach the `ALTER TABLE` branch — the fix would be inert on every existing
    deployment and the suite would still be green. This starts from the exact
    `seen` schema the previous version wrote, with a row in it.
    """
    import db
    import sqlite3

    path = tmp_path / "legacy.db"
    old = sqlite3.connect(path)
    old.execute(
        """CREATE TABLE seen (
            tenant_id TEXT NOT NULL DEFAULT '1',
            site TEXT NOT NULL,
            kind TEXT NOT NULL,
            item_id TEXT NOT NULL,
            payload TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tenant_id, site, kind, item_id)
        )"""
    )
    item = ff_email.parse("New message", msg(), received_at=ORIGINAL_DATE)
    old.execute("INSERT INTO seen (tenant_id, site, kind, item_id, payload) "
                "VALUES (?,?,?,?,?)",
                ("v155-legacy", SITE, "message", item["dedup_id"], "{}"))
    old.commit()
    old.close()

    monkeypatch.setattr(db, "DB_PATH", path)

    with storage._conn() as c:
        cols = db.table_columns(c, "seen")
        assert {"alt_id", "via_forward"} <= cols, "the migration did not run"
        kept = c.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    assert kept == 1, "the migration lost a row"

    # And the migrated row still behaves as a pre-upgrade row: alt_id is NULL,
    # so a redelivery of the message it stands for is still recognised.
    took, _ = ingest("v155-legacy", "New message", msg(), ORIGINAL_DATE)
    assert not took


def test_the_dedup_keys_are_not_written_into_the_stored_payload(tenant):
    """The stored payload must stay byte-identical to what the previous version
    wrote for the same message. It is not cosmetic: `filter_new` compares the
    fresh payload against the stored one to decide whether to rewrite the row,
    so a payload that gained two keys would mark every pre-existing row as
    changed on its next pass."""
    _took, item = ingest(tenant, "New message", msg(), ORIGINAL_DATE)
    assert "dedup_id" in item and "via_forward" in item

    with storage._conn() as c:
        payload = c.execute(
            "SELECT payload FROM seen WHERE tenant_id=? AND item_id=?",
            (tenant, item["id"]),
        ).fetchone()[0]
    assert "dedup_id" not in payload
    assert "via_forward" not in payload


def test_a_lead_is_keyed_exactly_as_before(tenant):
    """Leads are out of scope and their ids must not move — a changed lead id
    orphans a live deal. Pinned to the value `63f2df6` produces."""
    lead = ff_email.parse("New lead", """You have a new lead.

Property: Sunny 1BR
Traveler: Emma M.
Date received: July 19, 2026
Move in: 8/16/26
Move out: 7/16/27
Travelers: 3
Traveling with pets: yes
""", received_at="Sun, 19 Jul 2026 12:00:00 +0000")

    assert lead["id"] == "276789c98e374045"
    assert "dedup_id" not in lead, "a lead has one key and never needed a second"


# --- the recovery path reads the same rule as the webhook -------------------

def test_recovery_and_ingest_agree_about_a_re_forward(tenant):
    """`inbound.recover` asks `already_seen` before writing, where `store` asks
    `filter_new` as it writes. If those two answer differently, clicking Try
    again on a re-forward opens the second deal the webhook refused to."""
    assert ingest(tenant, "New message", msg(), ORIGINAL_DATE)[0]

    copy = ff_email.parse("Fwd: New message", fwd_gmail(msg()),
                          received_at=FORWARD_DATE)
    assert storage.already_seen(tenant, SITE, "message", copy["id"], item=copy)

    second = ff_email.parse("New message", msg(), received_at=SAME_DAY_LATER)
    assert not storage.already_seen(tenant, SITE, "message", second["id"],
                                    item=second), (
        "a genuine second message must still look unseen to recovery"
    )
