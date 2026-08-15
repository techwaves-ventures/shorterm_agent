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
          sender="no-reply@furnishedfinder.com", headers=None, recipient=None):
    return client.post(
        "/inbound/email",
        json={
            "recipient": recipient or inbound.address_for(tid),
            "from": sender,
            "subject": subject,
            "text": body,
        },
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
    assert "Couldn't find a guest name" in page, "reason in plain language"
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

    monkeypatch.setattr(ff_email, "parse", lambda subject, body: {
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

    monkeypatch.setattr(ff_email, "parse", lambda subject, body: {
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

    monkeypatch.setattr(ff_email, "parse", lambda subject, body: {
        "kind": "lead", "id": "boom-1", "title": "Boom | Emma", "url": "u",
        "source": "email", "raw": body, "traveler": "Emma", "property_name": "Boom",
    })

    def explode(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(inbound, "store", explode)

    resp = client.post(f"/inbound/rejected/{rid}/retry")
    assert resp.status_code == 302

    row = inbound_rejects.get(tid, SITE, rid)
    assert row["status"] == "open", "a failed store must not leave the row marked recovered"
    assert row["resolved_item_id"] in (None, ""), "must not claim a deal that was never opened"
    assert inbound_rejects.count_open(tid, SITE) == 1, "the lead must stay visible"


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

    monkeypatch.setattr(ff_email, "parse", lambda subject, body: {
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
    monkeypatch.setattr(ff_email, "parse", lambda subject, body: {
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
    assert "still could not parse" in row["reason"]
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
