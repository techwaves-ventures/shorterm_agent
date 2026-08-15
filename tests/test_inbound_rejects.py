"""A forwarded email we can't read must leave a record the operator can act on.

Every test here drives the *real* ingress — `POST /inbound/email` through the
Flask test client — rather than calling `inbound_rejects.record()` directly. A
previous ticket shipped a test that exercised a helper with an argument no
caller ever passes while the real call site was unreachable; going through HTTP
is what makes these tests evidence.

The two halves that matter:
  * a rejection past the provider secret is *kept* (the lost lead), and
  * a rejection before it is *not* (this endpoint is public — persisting probe
    traffic would turn it into an unauthenticated write amplifier).
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# --- Isolate DB + secrets BEFORE importing the app modules -----------------
_TMP = tempfile.mkdtemp(prefix="ven128_")
os.environ["SQLITE_PATH"] = str(Path(_TMP) / "test.db")
os.environ.pop("DATABASE_URL", None)  # force SQLite
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["INBOUND_EMAIL_DOMAIN"] = "inbound.example.com"
os.environ["INBOUND_WEBHOOK_SECRET"] = "provider-secret"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import dashboard  # noqa: E402
import inbound  # noqa: E402
import inbound_rejects  # noqa: E402
import models  # noqa: E402
import pipeline  # noqa: E402

SITE = "furnishedfinder"
SECRET = {"X-Inbound-Secret": "provider-secret"}
PASSWORD = "test-passphrase-123"

# What `inbound.extract_body` actually hands the parser for a digest: no guest
# name, no property, no dates. Derived from the real normalizer, not invented.
DIGEST = ("Your weekly FurnishedFinder digest. 3 new listings in your area "
          "this week. Log in to see them. Unsubscribe.")

# A real lead, for the cases that must still get through untouched.
GOOD_LEAD = """You have a new tenant lead.

Property: Quiet Spacious Home in NW DC - Unit 1
Traveler: Emma M.
Requested travel dates: Aug. 16, 2026 - Jul. 16, 2027
"""

_seq = iter(range(1, 10_000))


@pytest.fixture()
def client():
    dashboard.app.config["TESTING"] = True
    dashboard.app.config["WTF_CSRF_ENABLED"] = False
    return dashboard.app.test_client()


def _tenant():
    """A fresh tenant. Rows are tenant-scoped, so this isolates tests."""
    email = f"host{next(_seq)}@test.local"
    return models.create_user(email, PASSWORD).tenant_id


def _login(client, tenant_email):
    return client.post(
        "/login", data={"email": tenant_email, "password": PASSWORD}
    )


def _tenant_with_login(client):
    email = f"host{next(_seq)}@test.local"
    tid = models.create_user(email, PASSWORD).tenant_id
    # Otherwise /dashboard redirects a first-run tenant to the setup wizard.
    config.mark_onboarded(tid)
    _login(client, email)
    return tid


def _rows_everywhere() -> int:
    """Every reject row in the database, for any tenant.

    The "must not be persisted" tests count globally on purpose. Scoping them to
    the tenant under test made them blind: a change that filed pre-auth traffic
    under a synthetic tenant id wrote rows freely and still passed.
    """
    import db

    with db.connect() as c:
        try:
            return int(c.execute("SELECT COUNT(*) FROM inbound_rejects").fetchone()[0])
        except Exception:
            return 0  # table not created yet == no rows


def _post(client, tid, body=DIGEST, subject="Your weekly digest",
          sender="no-reply@furnishedfinder.com", headers=None, recipient=None,
          date=None):
    payload = {
        "recipient": recipient or inbound.address_for(tid),
        "from": sender,
        "subject": subject,
        "text": body,
    }
    # The provider's `Date`. Real forwards carry one; it is what tells two sends
    # of the same words apart, so tests that care about message identity must
    # send it rather than let it default to empty.
    if date is not None:
        payload["date"] = date
    return client.post(
        "/inbound/email",
        json=payload,
        headers=SECRET if headers is None else headers,
    )


# --- What must be kept -----------------------------------------------------


def test_unreadable_email_is_recorded_and_answer_is_still_a_flat_202(client):
    """The filed defect: this used to log a warning and vanish."""
    tid = _tenant()
    resp = _post(client, tid)

    assert resp.status_code == 202
    assert resp.get_data() == b"", "the flat 202 is what stops this leaking which addresses exist"

    rows = inbound_rejects.open_for_tenant(tid, SITE)
    assert len(rows) == 1, "an unreadable forward must leave a record"
    assert rows[0]["reason_code"] == "unparsed"
    assert rows[0]["subject"] == "Your weekly digest"
    assert rows[0]["sender"] == "no-reply@furnishedfinder.com"
    assert DIGEST[:30] in rows[0]["body"]


def test_sender_not_allowed_is_recorded(client):
    """A new FF sending domain would otherwise eat every lead in silence."""
    tid = _tenant()
    _post(client, tid, body=GOOD_LEAD, sender="alerts@new-ff-domain.com")

    rows = inbound_rejects.open_for_tenant(tid, SITE)
    assert len(rows) == 1
    assert rows[0]["reason_code"] == "sender_not_allowed"


def test_a_readable_lead_is_not_recorded_as_rejected(client):
    """Guard against the fix firing on the happy path."""
    tid = _tenant()
    resp = _post(client, tid, body=GOOD_LEAD, subject="New lead from Emma M.")

    assert resp.status_code == 202
    assert inbound_rejects.count_all(tid, SITE) == 0
    assert len(pipeline.all_deals(tid, SITE)) == 1, "the lead must still reach the board"


# --- What must NOT be kept (the endpoint is public) ------------------------


def test_bad_webhook_secret_stores_nothing(client):
    """Persisting pre-auth rejections would make a public endpoint a write amplifier."""
    tid = _tenant()
    before = _rows_everywhere()
    resp = _post(client, tid, body=DIGEST + " probe-bad-secret",
                 headers={"X-Inbound-Secret": "wrong"})

    assert resp.status_code == 202
    assert _rows_everywhere() == before, "a pre-auth rejection was persisted somewhere"


def test_unrecognised_recipient_stores_nothing(client):
    """No tenant resolved means no one to show it to — and no way to bound it."""
    tid = _tenant()
    before = _rows_everywhere()
    # A distinct body, so a row written under some other tenant can't be hidden
    # by deduping onto one another pre-auth test already created.
    resp = _post(
        client, tid, body=DIGEST + " probe-unknown-recipient",
        recipient="leads+999-deadbeefdeadbeef@inbound.example.com",
    )

    assert resp.status_code == 202
    assert _rows_everywhere() == before, "an unattributable rejection was persisted somewhere"


def test_the_allowlist_itself_gates_persistence_not_just_the_tenant_id(client, monkeypatch):
    """Guard the actual security control, not a side effect of it.

    The three cases above are also stopped by `Rejected.tenant_id` being unset,
    so they would still pass if `RECORDABLE_CODES` were widened to include the
    pre-auth codes. This drives a pre-auth rejection that *does* carry a tenant
    id, which only the code allowlist can refuse.
    """
    tid = _tenant()

    for code in ("bad_secret", "unknown_recipient", "too_large", "not_configured"):
        def reject(*a, _code=code, **kw):
            raise inbound.Rejected(f"simulated {_code}", code=_code, tenant_id=tid)

        before = _rows_everywhere()
        monkeypatch.setattr(inbound, "accept", reject)
        resp = _post(client, tid, body=f"{DIGEST} probe-{code}")

        assert resp.status_code == 202
        assert _rows_everywhere() == before, (
            f"{code} was persisted; only {inbound.RECORDABLE_CODES} may be"
        )


def test_recordable_codes_are_only_the_post_authentication_ones():
    """A code reaches this tuple only if the provider secret already verified."""
    assert set(inbound.RECORDABLE_CODES) == {"unparsed", "sender_not_allowed"}


def test_oversized_payload_stores_nothing(client):
    """Rejected on size before parsing, so it is never attributed or stored.

    The body is genuinely oversized rather than a forged Content-Length header,
    which the test client recomputes from the real payload.
    """
    tid = _tenant()
    before = _rows_everywhere()
    resp = _post(client, tid, body="x" * (inbound.MAX_PAYLOAD_BYTES + 1))

    assert resp.status_code == 202
    assert _rows_everywhere() == before, "an oversized payload was persisted somewhere"


# --- Bounds, because this content comes from outside -----------------------


def test_identical_replays_collapse_onto_one_row(client):
    tid = _tenant()
    for _ in range(3):
        _post(client, tid)

    rows = inbound_rejects.open_for_tenant(tid, SITE)
    assert len(rows) == 1, "a duplicate forward must not add a row"
    assert rows[0]["seen_count"] == 3


def test_row_count_is_capped_and_keeps_the_newest(client):
    tid = _tenant()
    total = inbound_rejects.MAX_ROWS_PER_TENANT + 5
    for i in range(total):
        _post(client, tid, body=f"{DIGEST} ref {i}", subject=f"digest {i}")

    rows = inbound_rejects.open_for_tenant(tid, SITE)
    assert len(rows) == inbound_rejects.MAX_ROWS_PER_TENANT
    subjects = {r["subject"] for r in rows}
    assert f"digest {total - 1}" in subjects, "newest must be kept"
    assert "digest 0" not in subjects, "oldest must be pruned"


def test_junk_at_the_cap_cannot_evict_the_genuine_lost_lead(client):
    """The cap must bound disk without discarding the evidence it exists to keep.

    A host who forwards *all* their mail instead of filtering on
    furnishedfinder.com generates `sender_not_allowed` rows at newsletter
    volume. Under a plain newest-wins cap those silently delete the one real
    unreadable enquiry — the loss this table exists to prevent, reintroduced by
    its own bookkeeping.
    """
    tid = _tenant()
    _post(client, tid, body="A guest wrote and we could not read it.",
          subject="New enquiry from a real guest")
    assert inbound_rejects.count_open(tid, SITE) == 1

    for i in range(inbound_rejects.MAX_ROWS_PER_TENANT + 10):
        _post(client, tid, body=f"Newsletter {i}", subject=f"Weekly roundup {i}",
              sender=f"news{i}@some-newsletter.com")

    rows = inbound_rejects.open_for_tenant(tid, SITE)
    assert len(rows) <= inbound_rejects.MAX_ROWS_PER_TENANT
    subjects = {r["subject"] for r in rows}
    assert "New enquiry from a real guest" in subjects, (
        "the real lost lead was evicted by junk — the cap defeated the feature"
    )


def test_a_deduped_row_is_kept_over_older_ones(client, monkeypatch):
    """A replay bumps `received_at` but not `id`; the cap must respect that.

    Ordering the survivors by id alone throws away the row being re-sent right
    now — the one most likely to be a guest trying again. The clock is driven
    explicitly because `received_at` has one-second resolution, and a test that
    writes 200 rows inside one second would tie on every comparison and prove
    nothing either way.
    """
    stamps = iter([f"2026-08-15T05:{m // 60:02d}:{m % 60:02d}+00:00" for m in range(1, 3000)])
    clock = {"t": next(stamps)}
    monkeypatch.setattr(inbound_rejects, "_now", lambda: clock["t"])

    tid = _tenant()
    _post(client, tid, body="Urgent, please read", subject="URGENT enquiry")

    # Fill to just under the cap, so the replay lands before any pruning.
    for i in range(inbound_rejects.MAX_ROWS_PER_TENANT - 2):
        clock["t"] = next(stamps)
        _post(client, tid, body=f"filler {i}", subject=f"filler {i}")

    # The guest re-forwards the original: same fingerprint, newest arrival.
    clock["t"] = "2026-08-15T09:00:00+00:00"
    _post(client, tid, body="Urgent, please read", subject="URGENT enquiry")
    assert inbound_rejects.get(tid, SITE,
        [r for r in inbound_rejects.open_for_tenant(tid, SITE)
         if r["subject"] == "URGENT enquiry"][0]["id"])["seen_count"] == 2

    # Now push past the cap; the oldest rows must go, not the freshest.
    for i in range(5):
        clock["t"] = f"2026-08-15T09:0{i + 1}:00+00:00"
        _post(client, tid, body=f"late {i}", subject=f"late {i}")

    subjects = {r["subject"] for r in inbound_rejects.open_for_tenant(tid, SITE)}
    assert "URGENT enquiry" in subjects, "the freshest row was evicted first"


def test_stored_body_is_truncated(client):
    tid = _tenant()
    _post(client, tid, body=DIGEST + ("A" * 40_000))

    row = inbound_rejects.open_for_tenant(tid, SITE)[0]
    assert len(row["body"]) <= inbound_rejects.MAX_STORED_BODY


def test_received_at_is_absolute_and_carries_an_offset(client):
    """A naive local stamp read on a second host is what double-sent a live message."""
    from datetime import datetime

    tid = _tenant()
    _post(client, tid)

    stamp = inbound_rejects.open_for_tenant(tid, SITE)[0]["received_at"]
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None, f"{stamp!r} has no offset — it means nothing on another host"
    assert parsed.utcoffset().total_seconds() == 0


# --- The operator-facing surface -------------------------------------------


def test_dashboard_banner_appears_only_when_something_was_lost(client):
    tid = _tenant_with_login(client)

    page = client.get("/dashboard").get_data(as_text=True)
    assert "couldn't be read" not in page, "no banner when nothing was lost"

    _post(client, tid)
    page = client.get("/dashboard").get_data(as_text=True)
    assert "couldn't be read" in page
    assert "/inbound/rejected" in page


def test_settings_states_what_happened_including_when_nothing_was_lost(client):
    """A host can only trust "nothing was lost" if it is stated, not implied by silence."""
    tid = _tenant_with_login(client)
    config.save_settings(tid, ingest_mode="email")

    page = client.get("/settings").get_data(as_text=True)
    assert "every forwarded email so far has been understood" in page

    _post(client, tid)
    page = client.get("/settings").get_data(as_text=True)
    assert "1 forwarded email" in page and "couldn't be read" in page

    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]
    client.post(f"/inbound/rejected/{rid}/dismiss")
    page = client.get("/settings").get_data(as_text=True)
    # Must not claim everything was understood — one was dismissed unread.
    assert "every forwarded email so far has been understood" not in page
    assert "nothing unread right now" in page


def test_the_banner_reports_the_actual_count(client):
    """A hardcoded number would satisfy a test that only greps for the wording."""
    tid = _tenant_with_login(client)
    for i in range(3):
        _post(client, tid, body=f"{DIGEST} ref {i}", subject=f"digest {i}")

    page = client.get("/dashboard").get_data(as_text=True)
    assert "3 forwarded emails" in page
    assert inbound_rejects.count_open(tid, SITE) == 3


def test_each_row_shows_what_the_operator_needs_to_act(client):
    """Sender, time, reason, an excerpt, and both actions — the whole point of the page."""
    tid = _tenant_with_login(client)
    _post(client, tid, body="A guest asked about the loft.", subject="An enquiry")
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    page = client.get("/inbound/rejected").get_data(as_text=True)

    assert "An enquiry" in page, "subject"
    assert "no-reply@furnishedfinder.com" in page, "sender"
    assert "UTC" in page, "received time"
    # Matched without the apostrophe: the reason is now the row's stored text
    # rather than markup in the template, so Jinja escapes it to `Couldn&#39;t`.
    assert "find a guest name" in page, "reason in plain language"
    assert "A guest asked about the loft." in page, "body excerpt"
    assert f"/inbound/rejected/{rid}/retry" in page, "retry action"
    assert f"/inbound/rejected/{rid}/dismiss" in page, "dismiss action"


def test_stored_content_is_escaped_not_executed(client):
    """The body is attacker-influenced and rendered into the operator's browser."""
    tid = _tenant_with_login(client)
    _post(client, tid, body=DIGEST + " <script>alert(1)</script>",
          subject="<img src=x onerror=alert(2)>")

    page = client.get("/inbound/rejected").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page, "stored XSS reached the DOM"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<img src=x onerror=alert(2)>" not in page


def test_retry_recovers_the_lead_and_cannot_create_two_deals(client, monkeypatch):
    """The day after a parser fix ships, the lost leads come back.

    The parser is patched to stand in for that deploy: the stored payload is
    unchanged, what changed is the code reading it.
    """
    tid = _tenant_with_login(client)
    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    from sites import ff_email

    monkeypatch.setattr(ff_email, "parse", lambda subject, body, received_at=None: {
        "kind": "lead", "id": "recovered-1", "title": "Recovered | Emma",
        "url": "https://example.test/lead", "source": "email", "raw": body,
        "traveler": "Emma", "property_name": "Recovered",
    })

    resp = client.post(f"/inbound/rejected/{rid}/retry")
    assert resp.status_code == 302
    assert len(pipeline.all_deals(tid, SITE)) == 1

    row = inbound_rejects.get(tid, SITE, rid)
    assert row["status"] == "recovered"
    assert row["resolved_item_id"] == "recovered-1"
    assert inbound_rejects.count_open(tid, SITE) == 0

    # Second press (a stale tab, a double-submit) must not open a second deal.
    # `storage.filter_new` would also swallow the duplicate, so that alone
    # proves nothing about the guard — see the dedicated test below.
    client.post(f"/inbound/rejected/{rid}/retry")
    assert len(pipeline.all_deals(tid, SITE)) == 1


def test_the_double_retry_guard_holds_without_help_from_storage_dedup(client, monkeypatch):
    """Prove the status claim stops the second retry, not `filter_new`.

    With dedup disabled, a missing claim shows up immediately as a second deal
    for the same guest — which is what the operator would actually see.
    """
    import storage

    tid = _tenant_with_login(client)
    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    from sites import ff_email

    monkeypatch.setattr(ff_email, "parse", lambda subject, body, received_at=None: {
        "kind": "lead", "id": "dedup-off-1", "title": "Twice | Emma", "url": "u",
        "source": "email", "raw": body, "traveler": "Emma", "property_name": "Twice",
    })
    # Every item looks brand new, so only the status claim can prevent a second deal.
    monkeypatch.setattr(storage, "filter_new", lambda t, s, k, items: list(items))

    client.post(f"/inbound/rejected/{rid}/retry")
    client.post(f"/inbound/rejected/{rid}/retry")

    assert len(pipeline.all_deals(tid, SITE)) == 1, "the second retry opened a duplicate deal"


def test_a_retry_whose_store_fails_hands_the_row_back(client, monkeypatch):
    """The row is claimed before the lead is stored; a failed store must undo that.

    Otherwise the message reads as recovered with nothing on the board — the
    silent loss this whole table exists to end, reintroduced by the fix.
    """
    tid = _tenant_with_login(client)
    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    from sites import ff_email

    monkeypatch.setattr(ff_email, "parse", lambda subject, body, received_at=None: {
        "kind": "lead", "id": "boom-1", "title": "Boom | Emma", "url": "u",
        "source": "email", "raw": body, "traveler": "Emma", "property_name": "Boom",
    })

    import pipeline as pipeline_mod

    def explode(*a, **kw):
        raise RuntimeError("database is locked")

    # Both routes to the board are down. Patching `store` alone is not enough to
    # prove this: recovery falls back to opening the deal directly, so the lead
    # would reach the board and `recovered` would be the honest answer.
    monkeypatch.setattr(inbound, "store", explode)
    monkeypatch.setattr(pipeline_mod, "ensure", explode)

    resp = client.post(f"/inbound/rejected/{rid}/retry")
    assert resp.status_code == 302

    assert len(pipeline.all_deals(tid, SITE)) == 0, "precondition: nothing reached the board"
    row = inbound_rejects.get(tid, SITE, rid)
    assert row["status"] == "open", "a failed store must not leave the row marked recovered"
    assert row["resolved_item_id"] in (None, ""), "must not claim a deal that was never opened"
    assert inbound_rejects.count_open(tid, SITE) == 1, "the lead must stay visible"


def test_recovering_a_reply_threads_it_instead_of_opening_a_second_deal(client, monkeypatch):
    """Recovery must go onto the board the same way ingest does — threading included.

    A message that continues a conversation joins that deal and never gets one of
    its own. Recovery that reaches for `pipeline.ensure` directly implements only
    half of that and opens a duplicate beside the thread: the owner sees the same
    guest twice and the reply carries none of the original's booking facts.

    The other half of the same mistake is the check: looking only for a deal keyed
    on the reply's own item id says "not on the board" for a reply that threaded
    perfectly, so the row is handed back and the operator is told it failed.
    """
    tid = _tenant_with_login(client)

    # A real lead opens the conversation.
    _post(client, tid, body=GOOD_LEAD, subject="New lead from Emma M.")
    deals = pipeline.all_deals(tid, SITE)
    assert len(deals) == 1, "precondition: one deal for Emma"
    parent = deals[0]

    # An unreadable forward lands on the list.
    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    # On retry it reads as Emma's reply to that same conversation.
    from sites import ff_email

    reply = {
        "kind": "message", "id": "reply-emma-1", "title": "Emma M.",
        "traveler": "Emma M.",
        "property_name": parent.get("property_name") or "Quiet Spacious Home in NW DC - Unit 1",
        "url": "u", "source": "email", "raw": "Is it still available?",
    }
    monkeypatch.setattr(ff_email, "parse", lambda subject, body, received_at=None: dict(reply))
    assert pipeline.find_thread(
        tid, SITE, pipeline.thread_key(reply), exclude_item_id=reply["id"]
    ), "precondition: the reply really does belong to Emma's thread"

    client.post(f"/inbound/rejected/{rid}/retry")

    after = pipeline.all_deals(tid, SITE)
    assert len(after) == 1, "a recovered reply must join its thread, not duplicate it"
    assert after[0]["item_id"] == parent["item_id"]
    assert inbound_rejects.get(tid, SITE, rid)["status"] == "recovered", (
        "a reply that threaded correctly must not be reported as still lost"
    )
    assert inbound_rejects.count_open(tid, SITE) == 0


def test_a_store_failure_alone_still_gets_the_guest_onto_the_board(client, monkeypatch):
    """`store` is a convenience, not the only way onto the board.

    It does dedup and deal-opening together, so a failure in its first half used
    to cost the lead entirely. Recovery opens the deal directly when it is
    missing, so the guest still arrives — losing the dedup bookkeeping, which
    `pipeline.ensure` is idempotent against, rather than losing the enquiry.
    """
    tid = _tenant_with_login(client)
    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    from sites import ff_email

    monkeypatch.setattr(ff_email, "parse", lambda subject, body, received_at=None: {
        "kind": "lead", "id": "half-1", "title": "Half | Emma", "url": "u",
        "source": "email", "raw": body, "traveler": "Emma", "property_name": "Half",
    })
    monkeypatch.setattr(inbound, "store", lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("database is locked")
    ))

    client.post(f"/inbound/rejected/{rid}/retry")

    assert len(pipeline.all_deals(tid, SITE)) == 1, "the guest must still reach the board"
    assert inbound_rejects.get(tid, SITE, rid)["status"] == "recovered"
    assert inbound_rejects.count_open(tid, SITE) == 0


def test_a_retry_whose_deal_never_opens_hands_the_row_back(client, monkeypatch):
    """`inbound.store` swallows a pipeline failure and still reports success.

    So "the call returned" is not evidence the guest reached the board. Without
    checking, the row is marked recovered, leaves the list, and points at a deal
    that does not exist — the silent loss wearing the fix's clothes.
    """
    import pipeline as pipeline_mod

    tid = _tenant_with_login(client)
    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    from sites import ff_email

    monkeypatch.setattr(ff_email, "parse", lambda subject, body, received_at=None: {
        "kind": "lead", "id": "gap-1", "title": "Gap | Emma", "url": "u",
        "source": "email", "raw": body, "traveler": "Emma", "property_name": "Gap",
    })

    def no_deal(*a, **kw):
        raise RuntimeError("pipeline is down")

    # Fails *inside* inbound.store, which logs it and returns True anyway.
    monkeypatch.setattr(pipeline_mod, "ensure", no_deal)

    client.post(f"/inbound/rejected/{rid}/retry")

    assert len(pipeline.all_deals(tid, SITE)) == 0, "precondition: no deal was opened"
    row = inbound_rejects.get(tid, SITE, rid)
    assert row["status"] == "open", "row marked recovered with nothing on the board"
    assert inbound_rejects.count_open(tid, SITE) == 1, "the lead must stay visible"


def test_a_retry_after_a_failed_one_still_recovers_the_lead(client, monkeypatch):
    """Handing the row back is only half a fix if the next retry can't work.

    `inbound.store` commits its dedup row *before* opening the deal and swallows
    the failure, so after one bad attempt the lead is marked seen with nothing on
    the board. Every later retry then short-circuits at that dedup and never
    reaches `pipeline.ensure` again: the row reopens forever and the guest can
    never be recovered from this page at all — permanent loss of exactly the lead
    this table promises to give back.
    """
    import pipeline as pipeline_mod

    tid = _tenant_with_login(client)
    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    from sites import ff_email

    monkeypatch.setattr(ff_email, "parse", lambda subject, body, received_at=None: {
        "kind": "lead", "id": "stuck-1", "title": "Stuck | Emma", "url": "u",
        "source": "email", "raw": body, "traveler": "Emma", "property_name": "Stuck",
    })

    # An outage that spans the whole first retry — `worker.py` writes the same
    # SQLite file, so a "database is locked" out of pipeline.ensure is the
    # realistic version. It must outlast the request, or the retry self-heals
    # and this never reaches the case being tested.
    down = {"yes": True}
    calls = {"n": 0}
    real_ensure = pipeline_mod.ensure

    def flaky(*a, **kw):
        calls["n"] += 1
        if down["yes"]:
            raise RuntimeError("database is locked")
        return real_ensure(*a, **kw)

    monkeypatch.setattr(pipeline_mod, "ensure", flaky)

    client.post(f"/inbound/rejected/{rid}/retry")
    assert inbound_rejects.get(tid, SITE, rid)["status"] == "open", (
        "precondition: the first attempt failed and handed the row back"
    )

    # The operator presses Try again. The outage is over.
    down["yes"] = False
    before = calls["n"]
    client.post(f"/inbound/rejected/{rid}/retry")

    assert calls["n"] > before, "the second retry never even attempted to open the deal"
    assert len(pipeline.all_deals(tid, SITE)) == 1, "the guest must reach the board"
    row = inbound_rejects.get(tid, SITE, rid)
    assert row["status"] == "recovered", "a recovered lead must leave the list"
    assert inbound_rejects.count_open(tid, SITE) == 0


def test_a_lead_recovered_on_a_later_retry_is_still_drafted(client, monkeypatch):
    """The draft gate must track the board, not `store`'s "was it new" answer.

    That answer is False on any retry after a half-completed store, which would
    leave a recovered guest sitting on the board with no reply written — a late
    lead needs the draft more, not less.
    """
    import pipeline as pipeline_mod
    import runner

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    tid = _tenant_with_login(client)
    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    from sites import ff_email

    monkeypatch.setattr(ff_email, "parse", lambda subject, body, received_at=None: {
        "kind": "lead", "id": "draft-1", "title": "Draft | Emma", "url": "u",
        "source": "email", "raw": body, "traveler": "Emma", "property_name": "Draft",
    })

    down = {"yes": True}
    real_ensure = pipeline_mod.ensure

    def flaky(*a, **kw):
        if down["yes"]:
            raise RuntimeError("database is locked")
        return real_ensure(*a, **kw)

    monkeypatch.setattr(pipeline_mod, "ensure", flaky)

    drafted = []
    monkeypatch.setattr(
        runner, "draft_ingested", lambda t, s, it: drafted.append(it.get("id"))
    )

    client.post(f"/inbound/rejected/{rid}/retry")   # fails, row handed back
    assert drafted == [], "precondition: nothing to draft while the deal never opened"
    down["yes"] = False
    client.post(f"/inbound/rejected/{rid}/retry")   # succeeds

    assert drafted == ["draft-1"], "a lead recovered on a later retry was never drafted"


def test_retrying_a_lead_already_on_the_board_does_not_duplicate_it(client, monkeypatch):
    """Recovery reuses the scrape path, so its dedup applies — verify, don't assume."""
    tid = _tenant_with_login(client)

    # A real lead arrives normally and opens a deal.
    _post(client, tid, body=GOOD_LEAD, subject="New lead from Emma M.")
    assert len(pipeline.all_deals(tid, SITE)) == 1
    existing = pipeline.all_deals(tid, SITE)[0]["item_id"]

    # The same guest also produced an unreadable forward that we recorded.
    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    from sites import ff_email

    # A fixed parser now reads it as the lead that is already on the board.
    monkeypatch.setattr(ff_email, "parse", lambda subject, body, received_at=None: {
        "kind": "lead", "id": existing, "title": "Dup | Emma", "url": "u",
        "source": "email", "raw": body, "traveler": "Emma M.", "property_name": "Dup",
    })

    client.post(f"/inbound/rejected/{rid}/retry")

    assert len(pipeline.all_deals(tid, SITE)) == 1, "recovery opened a duplicate deal"
    assert inbound_rejects.get(tid, SITE, rid)["status"] == "recovered"


def test_retry_that_still_fails_leaves_the_row_open(client):
    tid = _tenant_with_login(client)
    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    resp = client.post(f"/inbound/rejected/{rid}/retry")
    assert resp.status_code == 302

    row = inbound_rejects.get(tid, SITE, rid)
    assert row["status"] == "open", "an unrecoverable message must not disappear"
    assert "still couldn't read" in row["reason"]
    assert len(pipeline.all_deals(tid, SITE)) == 0


def test_dismiss_clears_the_list_but_keeps_the_record(client):
    tid = _tenant_with_login(client)
    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    client.post(f"/inbound/rejected/{rid}/dismiss")

    assert inbound_rejects.count_open(tid, SITE) == 0
    assert inbound_rejects.get(tid, SITE, rid)["status"] == "dismissed"
    assert inbound_rejects.count_all(tid, SITE) == 1, "the audit row must survive"


def test_a_dismissed_row_is_not_reopened_by_a_duplicate_forward(client):
    """Otherwise Dismiss is undone by the next replay and the list never clears."""
    tid = _tenant_with_login(client)
    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]
    client.post(f"/inbound/rejected/{rid}/dismiss")

    _post(client, tid)

    assert inbound_rejects.count_open(tid, SITE) == 0
    assert inbound_rejects.get(tid, SITE, rid)["seen_count"] == 2


# --- Isolation and CSRF ----------------------------------------------------


def test_another_tenant_cannot_see_or_touch_the_row(client):
    victim = _tenant()
    _post(client, victim)
    rid = inbound_rejects.open_for_tenant(victim, SITE)[0]["id"]

    attacker = _tenant_with_login(client)
    assert attacker != victim

    page = client.get("/inbound/rejected").get_data(as_text=True)
    assert "Your weekly digest" not in page, "another tenant's message was listed"

    assert client.post(f"/inbound/rejected/{rid}/retry").status_code == 404
    assert client.post(f"/inbound/rejected/{rid}/dismiss").status_code == 404
    assert inbound_rejects.get(victim, SITE, rid)["status"] == "open", "row was altered cross-tenant"


def test_the_review_page_requires_a_login(client):
    resp = client.get("/inbound/rejected")
    assert resp.status_code in (302, 401)
    assert "/login" in resp.headers.get("Location", "")


def test_retry_and_dismiss_require_a_csrf_token():
    """Without this, any page the host visits could clear their lost-lead queue."""
    dashboard.app.config["TESTING"] = True
    dashboard.app.config["WTF_CSRF_ENABLED"] = True
    try:
        client = dashboard.app.test_client()
        for path in ("/inbound/rejected/1/retry", "/inbound/rejected/1/dismiss"):
            assert client.post(path).status_code == 400, f"{path} accepted a tokenless POST"
    finally:
        dashboard.app.config["WTF_CSRF_ENABLED"] = False


# --- Portability (this passes on SQLite and breaks the hosted deploy) ------


def _executed_sql() -> list[str]:
    """Every literal SQL string this module hands to `.execute()`.

    Read from the AST rather than the raw text: the docstrings and comments
    discuss these traps by name, and a lint that greps the whole file would fire
    on the explanation of the bug instead of the bug.
    """
    import ast

    def literal(node):
        """The SQL text of a node, resolving f-strings built from module constants."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts = []
            for piece in node.values:
                if isinstance(piece, ast.Constant):
                    parts.append(str(piece.value))
                elif isinstance(piece, ast.FormattedValue) and isinstance(piece.value, ast.Name):
                    # e.g. f"{_SELECT} WHERE ..." — resolve from the live module.
                    parts.append(str(getattr(inbound_rejects, piece.value.id, "")))
                else:
                    return None  # unresolvable: caller must notice, not skip silently
            return "".join(parts)
        return None

    tree = ast.parse(Path(inbound_rejects.__file__).read_text())
    calls, out = 0, []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and node.args):
            calls += 1
            sql = literal(node.args[0])
            assert sql is not None, (
                f"could not resolve the SQL at line {node.lineno} — this lint would "
                f"skip it silently, which is how an unportable statement ships"
            )
            out.append(sql)
    # Every execute() must have been read, or the lint is blind to the difference.
    assert len(out) == calls, f"resolved {len(out)} of {calls} execute() call sites"
    return out


def test_sql_is_portable_to_postgres():
    """The hosted deploy is Postgres; CI is SQLite. These forms don't translate."""
    import db

    statements = _executed_sql()
    assert statements, "found no SQL to check — the AST walk is broken, not the module"

    for sql in statements:
        upper = sql.upper()
        assert "PRAGMA" not in upper, f"PRAGMA has no Postgres equivalent: {sql[:60]}"
        assert "INSERT OR REPLACE" not in upper, f"not valid Postgres: {sql[:60]}"
        assert "CURRENT_TIMESTAMP" not in upper, f"writes the server's naive local clock: {sql[:60]}"
        # `DELETE ... LIMIT` is accepted by SQLite and rejected by Postgres.
        if upper.lstrip().startswith("DELETE"):
            head = upper.split("SELECT")[0]
            assert "LIMIT" not in head, f"DELETE ... LIMIT is not valid Postgres: {sql[:60]}"

    src = Path(inbound_rejects.__file__).read_text()
    code = "\n".join(line.split("#")[0] for line in src.splitlines())
    assert "lastrowid" not in code, "Postgres has no cursor.lastrowid"

    # The translation layer must not mangle the statements we actually issue.
    translated = db._to_pg(
        "INSERT INTO inbound_rejects (tenant_id) VALUES (?) "
        "ON CONFLICT (tenant_id, site, fingerprint) DO UPDATE SET "
        "seen_count = inbound_rejects.seen_count + 1"
    )
    assert "?" not in translated
    assert translated.count("%s") == 1


# --- Regressions from the round-3 review (D1, D2, D3) -----------------------


def test_a_recovered_reply_whose_thread_write_fails_hands_the_row_back(client, monkeypatch):
    """D1: "on the board" is constant-True for a reply, so it cannot confirm one.

    The conversation a reply answers is on the board *before* the retry runs. A
    presence check therefore says "recovered" no matter what happened — the row
    leaves the queue, the guest's words were never written anywhere, and the
    operator is told the lead is safe. That is the silent loss this whole page
    exists to end, re-created inside the feature built to end it.
    """
    tid = _tenant_with_login(client)
    _post(client, tid, body=GOOD_LEAD, subject="New lead from Emma M.")
    deals = pipeline.all_deals(tid, SITE)
    assert len(deals) == 1, "precondition: one deal for Emma"
    parent = deals[0]

    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    from sites import ff_email

    reply = {
        "kind": "message", "id": "reply-emma-d1", "title": "Emma M.",
        "traveler": "Emma M.",
        "property_name": parent.get("property_name") or "Quiet Spacious Home in NW DC - Unit 1",
        "url": "u", "source": "email", "raw": "Any update?",
    }
    monkeypatch.setattr(ff_email, "parse",
                        lambda subject, body, received_at=None: dict(reply))

    # Both preconditions asserted before the act, so a pass cannot come from the
    # reply having been a non-thread, or from comparing two empty stamps.
    assert pipeline.find_thread(
        tid, SITE, pipeline.thread_key(reply), exclude_item_id=reply["id"]
    ), "precondition: the reply really does belong to Emma's thread"
    assert not pipeline.get(tid, SITE, parent["item_id"]).get("last_guest_reply_at"), (
        "precondition: the parent has recorded no guest reply yet"
    )

    import pipeline as pipeline_mod

    def explode(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(pipeline_mod, "record_guest_reply", explode)

    client.post(f"/inbound/rejected/{rid}/retry")

    # The reply genuinely did not land — the positive control for the assertions
    # below, so this cannot pass by the write having quietly succeeded.
    assert not pipeline.get(tid, SITE, parent["item_id"]).get("last_guest_reply_at"), (
        "control: the guest's reply really was never recorded"
    )
    assert len(pipeline.all_deals(tid, SITE)) == 1, "control: no deal was opened either"

    row = inbound_rejects.get(tid, SITE, rid)
    assert row["status"] == "open", (
        "a reply that never reached its thread must not read as recovered"
    )
    assert row["resolved_item_id"] in (None, ""), "must not claim a deal that was never opened"
    assert inbound_rejects.count_open(tid, SITE) == 1, "the lead must stay visible"


def test_a_reply_whose_thread_write_quietly_declines_hands_the_row_back(client, monkeypatch):
    """The same D1 harm by the failure mode that does *not* raise.

    Its sibling above makes `record_guest_reply` explode, which `recover` catches
    in its `except`. But the function's own documented "I did nothing" answer is a
    plain `None` return (`pipeline.py:496` — the parent deal no longer resolves),
    and that path never goes near the `except`. Round 5 replaced the old
    state-inference with `open_deal` *reporting* whether the board took the item,
    and this return value is the entire mechanism: if it ever reports success
    unconditionally, a reply that was never written is marked recovered and the
    operator is told the guest is safe.

    Without this test that mechanism is unguarded — making `open_deal` return a
    bare `True` keeps the whole suite green.
    """
    tid = _tenant_with_login(client)
    _post(client, tid, body=GOOD_LEAD, subject="New lead from Emma M.")
    deals = pipeline.all_deals(tid, SITE)
    assert len(deals) == 1, "precondition: one deal for Emma"
    parent = deals[0]

    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    from sites import ff_email

    reply = {
        "kind": "message", "id": "reply-emma-quiet", "title": "Emma M.",
        "traveler": "Emma M.",
        "property_name": parent.get("property_name") or "Quiet Spacious Home in NW DC - Unit 1",
        "url": "u", "source": "email", "raw": "Any update?",
    }
    monkeypatch.setattr(ff_email, "parse",
                        lambda subject, body, received_at=None: dict(reply))

    # Preconditions asserted before the act: this really is a threaded reply, and
    # the parent has no reply stamp yet — so a pass cannot come from the item not
    # being a thread, nor from comparing two empty values.
    assert pipeline.find_thread(
        tid, SITE, pipeline.thread_key(reply), exclude_item_id=reply["id"]
    ), "precondition: the reply really does belong to Emma's thread"
    assert not pipeline.get(tid, SITE, parent["item_id"]).get("last_guest_reply_at"), (
        "precondition: the parent has recorded no guest reply yet"
    )

    import pipeline as pipeline_mod

    calls = []

    def declines(*a, **kw):
        # Exactly what the real function returns when the deal has gone.
        calls.append(1)
        return None

    monkeypatch.setattr(pipeline_mod, "record_guest_reply", declines)

    client.post(f"/inbound/rejected/{rid}/retry")

    # Positive control: the code really did attempt the write and really did get
    # the declining answer, so a blind probe would be detectable here.
    assert calls, "control: the retry must actually have attempted the thread write"
    assert not pipeline.get(tid, SITE, parent["item_id"]).get("last_guest_reply_at"), (
        "control: the guest's reply really was never recorded"
    )
    assert len(pipeline.all_deals(tid, SITE)) == 1, "control: no deal was opened either"

    row = inbound_rejects.get(tid, SITE, rid)
    assert row["status"] == "open", (
        "a reply whose thread write declined must not read as recovered"
    )
    assert row["resolved_item_id"] in (None, ""), "must not claim a deal that was never opened"
    assert inbound_rejects.count_open(tid, SITE) == 1, "the lead must stay visible"

    # The item must not have been marked seen either: a recovery that did not
    # reach the board has to stay retryable, which is the ordering round 5
    # inverted (board write first, dedup only once it landed).
    import storage
    assert not storage.already_seen(tid, SITE, "message", reply["id"]), (
        "a failed recovery must leave nothing behind, so it can be retried"
    )


def test_two_sends_of_the_same_words_recover_onto_two_deals(client, monkeypatch):
    """D2: retry dropped the mail `Date`, so a re-send collapsed onto the first.

    A message id hashes a stamp that falls back to the mail date, and the body
    fingerprint strips quoted history — so a guest re-sending "Any update?" with
    the earlier exchange quoted underneath is separated from the original by the
    date alone. The webhook passes it; retry did not, so both rows resolved to
    one deal id and the second message was silently absorbed while the operator
    was told it had been recovered.

    Driven through the real parser (not a stub) because the id derivation is the
    thing under test, and through the real ingress for both capture and retry.
    """
    tid = _tenant_with_login(client)

    from sites import ff_email

    real_parse = ff_email.parse

    subject = "New message from Emma Rodriguez"
    words = ("Traveler: Emma Rodriguez\n"
             "Property: Quiet Spacious Home in NW DC - Unit 1\n\n"
             "Any update?\n")
    resend = words + "\n> On Aug 3 you wrote:\n> Thanks for reaching out, I will check.\n"

    # Pin our own capture clock so the two rows share a `received_at`. Without
    # this the test passes for the wrong reason: two captures a second apart get
    # different write times, and the fallback separates them even when the mail
    # date is never stored at all — so the assertion below would guard the
    # fallback rather than the `mail_date` column it is here to guard.
    monkeypatch.setattr(inbound_rejects, "_now",
                        lambda: "2026-08-15T09:00:00+00:00")

    # Before the parser fix: neither can be read, so both land on the review list.
    monkeypatch.setattr(ff_email, "parse", lambda *a, **kw: None)
    _post(client, tid, subject=subject, body=words,
          date="Mon, 3 Aug 2026 09:00:00 +0000")
    _post(client, tid, subject=subject, body=resend,
          date="Tue, 11 Aug 2026 09:00:00 +0000")
    assert len({r["received_at"] for r in inbound_rejects.open_for_tenant(tid, SITE)}) == 1, (
        "control: the write times are identical, so only mail_date can separate these"
    )

    rows = inbound_rejects.open_for_tenant(tid, SITE)
    assert len(rows) == 2, "precondition: two distinct rows, not one deduped row"

    # The parser fix ships and the operator retries both.
    monkeypatch.setattr(ff_email, "parse", real_parse)
    for r in rows:
        client.post(f"/inbound/rejected/{r['id']}/retry")

    resolved = [inbound_rejects.get(tid, SITE, r["id"]) for r in rows]
    assert all(x["status"] == "recovered" for x in resolved), (
        "control: both rows must actually have been recovered, or the ids below "
        "differ only because one retry failed"
    )
    ids = {x["resolved_item_id"] for x in resolved}
    assert len(ids) == 2, (
        "two different messages must not recover onto one deal id — the second "
        f"is silently absorbed. got {ids}"
    )
    # Same guest, same property, so the re-send correctly threads onto the first
    # message's conversation rather than opening a rival deal — but it has to
    # actually *land* there. Under the collision the second row resolved to the
    # first message's own id, `filter_new` called it already-seen, and nothing
    # was written anywhere while the row still read as recovered.
    deals = pipeline.all_deals(tid, SITE)
    assert len(deals) == 1, "one guest, one property: one conversation"
    assert deals[0].get("last_guest_reply_at"), (
        "the re-send must be recorded on the thread, not absorbed by the first"
    )


def test_retrying_an_already_ingested_reply_does_not_reply_to_the_guest_twice(client, monkeypatch):
    """A stale row whose message the webhook already ingested must be a no-op.

    Re-applying it is not harmless. `record_guest_reply` cancels the deal's
    queued follow-up and the drafting path then runs again — so one click on a
    row that is merely out of date cancels a scheduled nurture step and queues a
    *second* reply to a guest who only ever wrote once.

    Reachable without anything exotic: a `sender_not_allowed` row is captured,
    the operator fixes the allowlist, the guest's message is re-forwarded and
    ingested normally, and the original row is still sitting on the list.
    """
    tid = _tenant_with_login(client)
    _post(client, tid, body=GOOD_LEAD, subject="New lead from Emma M.")
    parent = pipeline.all_deals(tid, SITE)[0]

    # The row the operator will click, captured while the parser still failed.
    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    from sites import ff_email

    reply = {
        "kind": "message", "id": "reply-already-in", "title": "Emma M.",
        "traveler": "Emma M.",
        "property_name": parent.get("property_name") or "Quiet Spacious Home in NW DC - Unit 1",
        "url": "u", "source": "email", "raw": "Any update?",
    }
    monkeypatch.setattr(ff_email, "parse",
                        lambda subject, body, received_at=None: dict(reply))

    # The same message arrives properly and is ingested by the webhook.
    _post(client, tid, body="a second forward that now parses")
    assert not inbound.store(tid, dict(reply), SITE), (
        "precondition: the webhook really did ingest this message already"
    )

    # A nurture step is queued on the conversation.
    pipeline.update(tid, SITE, parent["item_id"],
                    next_action_at="2026-09-01T09:00:00", next_action_step=2)
    before = pipeline.get(tid, SITE, parent["item_id"])
    assert before["next_action_at"], "precondition: a follow-up is queued"

    import runner as runner_mod

    drafted = []
    monkeypatch.setattr(runner_mod, "draft_ingested",
                        lambda t, s, it: drafted.append(it.get("id")))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    client.post(f"/inbound/rejected/{rid}/retry")

    after = pipeline.get(tid, SITE, parent["item_id"])
    assert after["next_action_at"] == before["next_action_at"], (
        "retrying an already-ingested reply must not cancel the queued follow-up"
    )
    assert after["last_guest_reply_at"] == before["last_guest_reply_at"], (
        "the guest did not write again, so nothing may re-stamp the thread"
    )
    assert drafted == [], "must not queue a second reply to a guest who wrote once"
    assert inbound_rejects.get(tid, SITE, rid)["status"] == "recovered", (
        "the message really is on the board, so the row is genuinely resolved"
    )


def test_replies_recovered_in_the_same_second_all_land(client, monkeypatch):
    """Recovery must not depend on the wall clock ticking between two clicks.

    `pipeline._now()` is second-resolution, so anything that decides "did this
    land?" by comparing a stamp before and after the write reports failure for
    every recovery that shares a second with the previous one — handing back a
    row whose message reached the board perfectly well, and skipping its draft.
    """
    tid = _tenant_with_login(client)

    from sites import ff_email

    real_parse = ff_email.parse
    subject = "New message from Emma Rodriguez"
    words = ("Traveler: Emma Rodriguez\n"
             "Property: Quiet Spacious Home in NW DC - Unit 1\n\n"
             "Any update?\n")

    monkeypatch.setattr(ff_email, "parse", lambda *a, **kw: None)
    for n, day in enumerate(("3", "11", "19"), start=1):
        _post(client, tid, subject=subject,
              body=words + ("\n> quoted history %s\n" % ("x" * n)),
              date=f"Mon, {day} Aug 2026 09:00:00 +0000")
    rows = inbound_rejects.open_for_tenant(tid, SITE)
    assert len(rows) == 3, "precondition: three distinct rows"

    # Back to back, deliberately with no sleep — they share a wall-clock second.
    monkeypatch.setattr(ff_email, "parse", real_parse)
    for r in rows:
        client.post(f"/inbound/rejected/{r['id']}/retry")

    out = [inbound_rejects.get(tid, SITE, r["id"]) for r in rows]
    stuck = [(o["id"], o["reason"]) for o in out if o["status"] != "recovered"]
    assert not stuck, f"every message reached the board, so none may be handed back: {stuck}"


# --- Round 7: the review found the `open_deal` refactor left three callers
# --- still inferring success from presence. These pin each one. --------------


def test_a_dedup_write_that_fails_after_the_board_took_the_lead_is_not_a_loss(
        client, monkeypatch):
    """F1: the lead is on the board, so the operator must not be told it is lost.

    `recover` marks the item seen *after* the board write, which is the right
    order — but that write sat outside the `try`, so anything it raised was
    caught by the route and turned into "couldn't open the lead — try again."
    `worker.py` shares the SQLite file, so `database is locked` is the ordinary
    way for it to raise.

    That is the exact inverse of the lie the boolean was introduced to remove:
    the lead is on the board, the row is handed back saying it isn't, and the
    draft never runs. It is also what produces F2 — the operator does what the
    message says, clicks again, and the guest's single reply is applied twice.
    """
    import storage

    tid = _tenant_with_login(client)
    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    from sites import ff_email

    monkeypatch.setattr(ff_email, "parse", lambda subject, body, received_at=None: {
        "kind": "lead", "id": "post-board-fail", "title": "Locked | Emma",
        "url": "u", "source": "email", "raw": body, "traveler": "Emma",
        "property_name": "Locked",
    })

    real_filter_new = storage.filter_new

    def locked(tenant_id, site, kind, items):
        raise Exception("database is locked")

    monkeypatch.setattr(storage, "filter_new", locked)

    import runner as runner_mod

    drafted = []
    monkeypatch.setattr(runner_mod, "draft_ingested",
                        lambda t, s, it: drafted.append(it.get("id")))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    client.post(f"/inbound/rejected/{rid}/retry")

    deals = pipeline.all_deals(tid, SITE)
    assert len(deals) == 1 and deals[0]["item_id"] == "post-board-fail", (
        "control: the board write itself succeeded, so this test is about what "
        "we *say* about it"
    )
    row = inbound_rejects.get(tid, SITE, rid)
    assert row["status"] == "recovered", (
        "the lead reached the board; a failed dedup write afterwards is "
        f"bookkeeping, not a lost lead. got status={row['status']!r} "
        f"reason={row['reason']!r}"
    )
    assert drafted == ["post-board-fail"], (
        "a recovered lead still needs its draft — the whole point of recovering it"
    )

    # F2: with the row correctly resolved there is no instruction to click
    # again, and the claim stops a stale tab from applying it a second time.
    monkeypatch.setattr(storage, "filter_new", real_filter_new)
    client.post(f"/inbound/rejected/{rid}/retry")
    assert len(pipeline.all_deals(tid, SITE)) == 1, "a second click must not re-apply"


def test_an_ingest_that_never_reached_the_board_leaves_no_seen_row(client, monkeypatch):
    """F3: `store` marked the item seen and swallowed the board failure.

    That flag is what the retry path reads. Leaving it set for something that
    reached nothing means the message is unreachable forever — every later
    delivery short-circuits at the dedup — and the review page reports the lead
    as safe on the strength of the same flag.

    The scenario is this feature's own advertised one: an unreadable forward is
    captured, the parser fix ships, the email is re-delivered, that delivery hits
    one transient board failure, and the operator clicks Try again.
    """
    import storage

    tid = _tenant_with_login(client)

    # The conversation the reply belongs to.
    _post(client, tid, body=GOOD_LEAD, subject="New lead from Emma M.")
    parent = pipeline.all_deals(tid, SITE)[0]

    # Captured while the parser still failed.
    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    from sites import ff_email

    reply = {
        "kind": "message", "id": "reply-lost-in-store", "title": "Emma M.",
        "traveler": "Emma M.",
        "property_name": parent.get("property_name") or "Quiet Spacious Home in NW DC - Unit 1",
        "url": "u", "source": "email", "raw": "Any update?",
    }
    monkeypatch.setattr(ff_email, "parse",
                        lambda subject, body, received_at=None: dict(reply))

    # The parser fix ships and the email is re-delivered — but the board write
    # declines this once. `record_guest_reply` returning None is its ordinary
    # way of declining (the deal wasn't found), and it does not raise.
    calls = {"n": 0}
    real_rgr = pipeline.record_guest_reply

    def flaky(tenant_id, site, item_id, at=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_rgr(tenant_id, site, item_id, at)

    monkeypatch.setattr(pipeline, "record_guest_reply", flaky)
    _post(client, tid, body="the same email, now parseable")

    assert calls["n"] == 1, "control: the re-delivery really did attempt the board write"
    assert not storage.already_seen(tid, SITE, "message", "reply-lost-in-store"), (
        "nothing reached the board, so nothing may claim this message was handled "
        "— that flag is what every later delivery and the retry page both read"
    )

    # Try again. The board write works this time, so the reply must actually land.
    client.post(f"/inbound/rejected/{rid}/retry")

    after = pipeline.get(tid, SITE, parent["item_id"])
    assert after["last_guest_reply_at"], (
        "the guest's reply must be recorded on the thread — without this the "
        "row still reads 'recovered' while the nurture sequence keeps chasing "
        "someone who already wrote back"
    )
    assert inbound_rejects.get(tid, SITE, rid)["status"] == "recovered"


def test_a_row_whose_thread_has_since_closed_can_still_be_cleared(client, monkeypatch):
    """F5: 'is the parent still open?' is not 'did this message land?'.

    Same presence test as F3, failing the other way. The message *was* ingested;
    the deal it joined has since completed, so `find_thread` no longer returns
    it, and the row is handed back forever asserting a failure that never
    happened. No injected fault anywhere in this test.
    """
    tid = _tenant_with_login(client)

    _post(client, tid, body=GOOD_LEAD, subject="New lead from Emma M.")
    parent = pipeline.all_deals(tid, SITE)[0]

    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    from sites import ff_email

    reply = {
        "kind": "message", "id": "reply-then-completed", "title": "Emma M.",
        "traveler": "Emma M.",
        "property_name": parent.get("property_name") or "Quiet Spacious Home in NW DC - Unit 1",
        "url": "u", "source": "email", "raw": "Any update?",
    }
    monkeypatch.setattr(ff_email, "parse",
                        lambda subject, body, received_at=None: dict(reply))

    # Re-delivered and ingested cleanly.
    _post(client, tid, body="the same email, now parseable")
    import storage
    assert storage.already_seen(tid, SITE, "message", "reply-then-completed"), (
        "precondition: this message really was ingested"
    )

    # The stay happens and the deal completes.
    pipeline.update(tid, SITE, parent["item_id"], stage=pipeline.COMPLETED)
    assert pipeline.find_thread(tid, SITE, pipeline.thread_key(reply)) is None, (
        "precondition: the thread is no longer open, so presence can't answer"
    )

    # The operator clears the stale row.
    for _ in range(3):
        client.post(f"/inbound/rejected/{rid}/retry")

    row = inbound_rejects.get(tid, SITE, rid)
    assert row["status"] == "recovered", (
        "this message was ingested and the operator has no way to change that; "
        "a row no click can ever clear is the page telling them to keep trying "
        f"at nothing. got status={row['status']!r} reason={row['reason']!r}"
    )


def test_retry_derives_the_same_message_id_the_webhook_did_without_a_date(
        client, monkeypatch):
    """F4: falling back to our own write clock broke parity with the webhook.

    `extract_date` returns `""` when the payload carries no usable `Date`, so
    that is exactly what the webhook hands the parser. Substituting
    `received_at` — a clock the webhook never sees — derives a *different* id
    for the same email, so a later re-delivery of it is no longer recognised as
    a duplicate: the guest's single reply is applied to the thread twice and a
    second autopilot answer is queued to someone who wrote once.

    A *message* id is what hashes the stamp; a lead's does not, so this has to
    be driven with a guest reply or it tests nothing.
    """
    import storage

    tid = _tenant_with_login(client)

    from sites import ff_email

    real_parse = ff_email.parse

    # The conversation the reply belongs to.
    _post(client, tid, body=GOOD_LEAD, subject="New lead from Emma M.")
    parent = pipeline.all_deals(tid, SITE)[0]

    subject = "New message from Emma M."
    body = ("Traveler: Emma M.\n"
            "Property: Quiet Spacious Home in NW DC - Unit 1\n\n"
            "Any update?\n")

    # Captured with no `Date` at all — the shape that makes the two paths differ.
    monkeypatch.setattr(ff_email, "parse", lambda *a, **kw: None)
    _post(client, tid, subject=subject, body=body)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]
    assert not inbound_rejects.get(tid, SITE, rid)["mail_date"], (
        "precondition: no stored mail date, so the fallback is what is under test"
    )

    # The parser fix ships and the same email is re-forwarded, ingesting normally.
    monkeypatch.setattr(ff_email, "parse", real_parse)
    _post(client, tid, subject=subject, body=body)
    msgs = storage.get_recent(tid, SITE, "message", 10)
    assert len(msgs) == 1, "precondition: the re-forward ingested exactly one message"
    webhook_id = msgs[0]["id"]

    stamped = pipeline.get(tid, SITE, parent["item_id"])["last_guest_reply_at"]
    assert stamped, "precondition: the reply reached the thread"

    import runner as runner_mod

    drafted = []
    monkeypatch.setattr(runner_mod, "draft_ingested",
                        lambda t, s, it: drafted.append(it.get("id")))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    # Now the operator retries the stale row for that same email.
    client.post(f"/inbound/rejected/{rid}/retry")

    assert inbound_rejects.get(tid, SITE, rid)["resolved_item_id"] == webhook_id, (
        "retry must derive the id the webhook derived for the same email, or the "
        "two paths stop deduplicating each other"
    )
    assert len(storage.get_recent(tid, SITE, "message", 10)) == 1, (
        "one email must not become two ingested messages"
    )
    assert drafted == [], (
        "the guest wrote once; a second autopilot reply must not be queued"
    )


def test_a_lead_the_board_did_not_take_is_reported_as_not_taken(client, monkeypatch):
    """F9: nothing exercised `open_deal`'s read-back on the *lead* path.

    Round 6 set out to guard exactly this and guarded only the reply half —
    replacing the read-back with `return True` kept the whole suite green. It is
    defensive (`pipeline.ensure` raises on failure), but a defence no test
    touches is a defence that quietly stops working.
    """
    import storage

    tid = _tenant_with_login(client)
    _post(client, tid)
    rid = inbound_rejects.open_for_tenant(tid, SITE)[0]["id"]

    from sites import ff_email

    monkeypatch.setattr(ff_email, "parse", lambda subject, body, received_at=None: {
        "kind": "lead", "id": "never-written", "title": "Ghost | Emma", "url": "u",
        "source": "email", "raw": body, "traveler": "Emma", "property_name": "Ghost",
    })
    # `ensure` returns without writing anything — the one thing the read-back
    # exists to notice.
    monkeypatch.setattr(pipeline, "ensure", lambda *a, **kw: None)

    assert not inbound.open_deal(tid, {"kind": "lead", "id": "never-written",
                                       "traveler": "Emma"}, SITE), (
        "the board has no such deal, so `open_deal` must not claim it took one"
    )

    client.post(f"/inbound/rejected/{rid}/retry")

    row = inbound_rejects.get(tid, SITE, rid)
    assert row["status"] == "open", (
        "nothing reached the board, so the row must stay on the operator's list"
    )
    assert not storage.already_seen(tid, SITE, "lead", "never-written"), (
        "a failed attempt must leave nothing behind, or no later retry can work"
    )


def test_a_failed_retry_keeps_the_diagnosis_the_operator_can_act_on(client, monkeypatch):
    """F8: a `sender_not_allowed` row's real problem is the allowlist.

    Every retry ends at the parser, so writing parse-flavoured copy
    unconditionally took the one actionable fact off the host's screen and
    replaced it with one they cannot do anything about.
    """
    tid = _tenant_with_login(client)
    _post(client, tid, body=GOOD_LEAD, sender="alerts@new-ff-domain.com")
    row = inbound_rejects.open_for_tenant(tid, SITE)[0]
    assert row["reason_code"] == "sender_not_allowed"

    from sites import ff_email

    monkeypatch.setattr(ff_email, "parse", lambda *a, **kw: None)
    client.post(f"/inbound/rejected/{row['id']}/retry")

    reason = inbound_rejects.get(tid, SITE, row["id"])["reason"]
    assert "don't recognise" in reason, (
        f"the allowlist is what the host can fix; got {reason!r}"
    )


def test_the_mail_date_migration_survives_losing_the_race(monkeypatch):
    """F6: two workers can both try the `ALTER`, and the loser must not raise.

    On Postgres the loser's `DuplicateColumn` aborts the whole transaction —
    including the `record` upsert about to run on the same connection — so the
    endpoint answers 202 with the lead dropped. Simulated here by making the
    column check miss what is already there, which is precisely what the racing
    worker saw when it read.
    """
    import db

    real_table_columns = db.table_columns
    seen = {"n": 0}

    def blind_once(conn, table):
        cols = real_table_columns(conn, table)
        if table == "inbound_rejects" and seen["n"] == 0:
            seen["n"] += 1
            return {c for c in cols if c != "mail_date"}
        return cols

    monkeypatch.setattr(db, "table_columns", blind_once)

    # Must not raise, and must leave a usable connection behind it.
    with inbound_rejects._conn() as c:
        assert "mail_date" in real_table_columns(c, "inbound_rejects")
        c.execute("SELECT COUNT(*) FROM inbound_rejects").fetchone()
    assert seen["n"] == 1, "control: the ALTER really was attempted"
