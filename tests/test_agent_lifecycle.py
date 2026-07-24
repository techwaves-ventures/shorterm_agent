"""Tests for the pieces that can message a real guest or spend real money.

These modules were untested and are the ones with consequences: the outbox is
the only gate between a draft and a guest's inbox, the scheduler decides when
the system acts unattended, and pipeline decides what the owner is shown as
needing them. A regression here is a wrong message to a customer's customer.
"""
import json
import os
import tempfile
from datetime import datetime, timedelta

import pytest
from zoneinfo import ZoneInfo

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("FF_CRED_KEY", "c9jwUi0L-fUjf3wjbq74M0lK3ah7fmEfGhjxZ7RehQk=")
os.environ.setdefault("SECRET_KEY", "test-secret")

import config  # noqa: E402
import digest  # noqa: E402
import models  # noqa: E402
import outbox  # noqa: E402
import pipeline  # noqa: E402
import scheduler  # noqa: E402
import sequences  # noqa: E402
import storage  # noqa: E402

SITE = "furnishedfinder"
UTC = ZoneInfo("UTC")


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    """A clean tenant on its own database file."""
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    tid = "1"
    config.save_settings(tid, host_name="Test Host", timezone="America/New_York",
                         digest_enabled="1", digest_hour="18:00",
                         autopilot="0", check_times="09:00,16:00", last_check_at="",
                         last_digest_at="")
    return tid


def _lead(tenant_id, item_id="L1", received="July 20, 2026", **extra):
    item = {"id": item_id, "kind": "lead", "traveler": "Dana R.",
            "title": "Unit 1 | Washington, District of Columbia | Dana R.",
            "received_at": received, **extra}
    storage.filter_new(tenant_id, SITE, "lead", [item])
    pipeline.ensure(tenant_id, SITE, item, None)
    return item


# --- pipeline ---------------------------------------------------------------


def test_inquiry_date_prefers_detail_over_row(tenant):
    """The row's `received` can actually be the move-OUT date; a future date is
    never an arrival time and must not set the urgency clock."""
    assert pipeline.inquiry_date({"received_at": "July 18, 2026"}) == "2026-07-18"
    assert pipeline.inquiry_date({"detail": "Date received:\n\nJuly 18, 2026"}) == "2026-07-18"
    # Row value in the future (it's the move-out date) → rejected.
    assert pipeline.inquiry_date({"received": "8/31/29"}) is None
    # Past row value with nothing better → used.
    assert pipeline.inquiry_date({"received": "1/15/26"}) == "2026-01-15"


def test_needs_action_excludes_clean_skips_but_keeps_draft_errors(tenant):
    _lead(tenant, "keep")
    _lead(tenant, "skipme")
    _lead(tenant, "errored")
    deals = pipeline.all_deals(tenant, SITE)
    responses = {
        "skipme": {"status": "skipped", "reason": "Budget below the unit price."},
        "errored": {"status": "skipped", "reason": "draft error: API key missing"},
    }
    ids = {d["item_id"] for d in pipeline.needs_action(deals, responses)}
    assert "keep" in ids           # never evaluated → needs a human
    assert "errored" in ids        # a failure, not a decision → needs a human
    assert "skipme" not in ids     # the agent handled it → oversight list only
    assert {d["item_id"] for d in pipeline.reviewable(deals, responses)} == {"skipme"}


def test_needs_action_sorted_oldest_waiting_first(tenant):
    _lead(tenant, "new", received="July 20, 2026")
    _lead(tenant, "old", received="July 12, 2026")
    deals = pipeline.all_deals(tenant, SITE)
    order = [d["item_id"] for d in pipeline.needs_action(deals, {})]
    assert order == ["old", "new"], "longest-waiting guest must come first"


def test_stale_leads_drop_out_of_the_queue(tenant):
    _lead(tenant, "ancient", received="January 15, 2026")
    deals = pipeline.all_deals(tenant, SITE)
    assert pipeline.needs_action(deals, {}) == []


# --- outbox: the gate between a draft and a guest ---------------------------


def test_manual_message_waits_for_approval(tenant):
    _lead(tenant)
    msg = outbox.add(tenant, SITE, "L1", sequence="presale", step_id="intro",
                     step_label="First reply", body="Hi", auto=False)
    assert msg["status"] == outbox.PENDING, "un-approved copy must never be sendable"
    assert outbox.next_queued(tenant) is None


def test_approval_releases_and_can_edit_the_text(tenant):
    _lead(tenant)
    msg = outbox.add(tenant, SITE, "L1", sequence="presale", step_id="intro",
                     step_label="First reply", body="original", auto=False)
    outbox.approve(msg["id"], "edited by the host")
    released = outbox.next_queued(tenant)
    assert released["status"] == outbox.QUEUED
    assert released["body"] == "edited by the host"


def test_cancelled_message_is_never_delivered(tenant):
    _lead(tenant)
    msg = outbox.add(tenant, SITE, "L1", sequence="presale", step_id="intro",
                     step_label="First reply", body="Hi", auto=False)
    outbox.cancel(msg["id"])
    assert outbox.next_queued(tenant) is None


def test_step_is_not_drafted_twice(tenant):
    _lead(tenant)
    outbox.add(tenant, SITE, "L1", sequence="presale", step_id="followup_1",
               step_label="Follow-up #1", body="nudge", auto=False)
    assert outbox.has_open_step(tenant, SITE, "L1", "followup_1")
    assert not outbox.has_open_step(tenant, SITE, "L1", "followup_2")


def test_stuck_sending_is_requeued_not_lost(tenant):
    _lead(tenant)
    msg = outbox.add(tenant, SITE, "L1", sequence="presale", step_id="intro",
                     step_label="First reply", body="Hi", auto=True)
    outbox.set_status(msg["id"], outbox.SENDING)
    assert outbox.next_queued(tenant) is None
    # A process that died mid-send would strand this forever otherwise.
    assert outbox.reclaim_stuck_sending(max_age_seconds=-1) == 1
    assert outbox.next_queued(tenant)["id"] == msg["id"]


# --- sequences: the safety rails --------------------------------------------


def test_credential_bearing_step_can_never_be_armed():
    step = sequences.find_step(sequences.PREARRIVAL, "check_in_details")
    assert step["never_auto"] is True
    # Even if a settings write somehow lists it, it must not auto-send: a
    # hallucinated or leaked door code is a real-world security incident.
    assert not sequences.can_auto_send(step, {"check_in_details"})
    assert "check_in_details" not in sequences.default_enabled_steps()


def test_presale_never_auto_sends_by_default():
    for step in sequences.steps(sequences.PRESALE):
        assert not step.get("auto_send_default"), step["id"]


def test_prearrival_scheduling_is_anchored_to_check_in():
    deal = {"check_in": "2026-09-01", "check_out": "2026-12-01",
            "inquiry_at": "2026-07-01T09:00:00", "last_contact_at": None}
    week_before = sequences.find_step(sequences.PREARRIVAL, "pre_arrival_week")
    assert sequences.due_at(deal, week_before).startswith("2026-08-25")
    # Unschedulable without the anchor rather than guessing a date.
    assert sequences.due_at({"check_in": None}, week_before) is None


def test_quiet_hours_push_sends_out_of_the_night():
    deal = {"check_in": "2026-09-01", "inquiry_at": "2026-07-01T09:00:00"}
    welcome = sequences.find_step(sequences.PREARRIVAL, "welcome")
    when = sequences.due_at(deal, welcome)
    hour = int(when[11:13])
    assert sequences.QUIET_START.hour <= hour <= sequences.QUIET_END.hour


# --- scheduler: acting unattended -------------------------------------------


def test_autopilot_off_never_fires(tenant):
    config.save_settings(tenant, autopilot="0")
    assert scheduler.due_slot(tenant, datetime(2026, 7, 22, 14, 0, tzinfo=UTC)) is None


def test_slot_fires_once_per_day(tenant):
    # Browser mode explicitly: email ingestion is never scheduled for a scrape
    # (see test_email_mode_is_never_scheduled_for_a_scrape).
    config.save_settings(tenant, autopilot="1", check_times="09:00",
                         ingest_mode=config.INGEST_BROWSER)
    # 14:00 UTC = 10:00 New York, past the 09:00 slot.
    now = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)
    assert scheduler.due_slot(tenant, now) is not None
    scheduler.mark_checked(tenant, now)
    assert scheduler.due_slot(tenant, now) is None
    assert scheduler.due_slot(tenant, now + timedelta(hours=2)) is None
    assert scheduler.due_slot(tenant, now + timedelta(days=1)) is not None


def test_out_of_hours_times_are_rejected():
    assert scheduler.parse_times("03:00") == []
    assert scheduler.parse_times("25:00,nonsense") == []
    assert len(scheduler.parse_times("09:00,16:00")) == 2


def test_digest_follows_property_timezone_not_server(tenant):
    """18:00 must mean 18:00 where the property is."""
    config.save_settings(tenant, digest_hour="18:00", last_digest_at="")
    before = datetime(2026, 7, 22, 21, 30, tzinfo=UTC)  # 17:30 New York
    after = datetime(2026, 7, 22, 22, 30, tzinfo=UTC)   # 18:30 New York
    assert not scheduler.digest_due(tenant, before)
    assert scheduler.digest_due(tenant, after)


def test_digest_sends_once_per_day(tenant):
    now = datetime(2026, 7, 22, 22, 30, tzinfo=UTC)
    assert scheduler.digest_due(tenant, now)
    scheduler.mark_digest_sent(tenant, now)
    assert not scheduler.digest_due(tenant, now)
    assert scheduler.digest_due(tenant, now + timedelta(days=1))


# --- digest -----------------------------------------------------------------


def test_digest_is_silent_when_there_is_nothing_to_report(tenant):
    assert digest.build(tenant) is None


def test_digest_leads_with_failures_then_waiting_guests(tenant):
    _lead(tenant)
    msg = outbox.add(tenant, SITE, "L1", sequence="presale", step_id="intro",
                     step_label="First reply", body="Hi", auto=True)
    outbox.set_status(msg["id"], outbox.FAILED, error="Could not find lead row")
    built = digest.build(tenant)
    assert "failed" in built["subject"].lower()
    assert "Could not find lead row" in built["body"]
    assert "Dana R." in built["body"]


# --- catalog enrichment from scraped listings -------------------------------


def _listing(tenant_id, name="Sunny House", price=4150, area="Washington, DC"):
    item = {"id": "P1", "kind": "property", "name": name, "title": name,
            "monthly_price": price, "area": area,
            "images": ["https://example.com/a-full.jpg"]}
    storage.filter_new(tenant_id, SITE, "property", [item])
    return item


def test_enrichment_only_offers_missing_facts(tenant):
    _listing(tenant)
    config.save_settings(tenant, units_json=json.dumps([
        {"id": "u1", "name": "Sunny House - Unit 1"},                     # missing both
        {"id": "u2", "name": "Sunny House - Unit 2", "monthly_price": 1800},  # price set
    ]))
    by_unit = {s["unit_id"]: s for s in config.suggested_enrichments(tenant, SITE)}
    assert set(by_unit["u1"]["fields"]) == {"monthly_price", "area"}
    # A price the host already set is theirs — never proposed for overwrite.
    assert "monthly_price" not in by_unit["u2"]["fields"]
    assert by_unit["u2"]["fields"] == {"area": "Washington, DC"}


def test_whole_property_price_is_flagged_when_shared(tenant):
    """FurnishedFinder prices the property, not the room. Copying that onto each
    sub-unit would have the agent quote the full-house rate for one unit."""
    _listing(tenant)
    config.save_settings(tenant, units_json=json.dumps([
        {"id": "u1", "name": "Sunny House - Unit 1"},
        {"id": "u2", "name": "Sunny House - Unit 2"},
    ]))
    assert all(s["shared"] for s in config.suggested_enrichments(tenant, SITE))

    config.save_settings(tenant, units_json=json.dumps([{"id": "u1", "name": "Sunny House"}]))
    assert all(not s["shared"] for s in config.suggested_enrichments(tenant, SITE))


def test_enrichment_applies_only_what_was_selected(tenant):
    _listing(tenant)
    config.save_settings(tenant, units_json=json.dumps([
        {"id": "u1", "name": "Sunny House - Unit 1"},
    ]))
    assert config.apply_enrichments(tenant, ["u1:area"], SITE) == 1
    units = {u["id"]: u for u in config.get_units(tenant)}
    assert units["u1"]["area"] == "Washington, DC"
    assert "monthly_price" not in units["u1"], "unticked field must not be written"


def test_enrichment_ignores_values_posted_by_the_browser(tenant):
    """Suggestions are re-derived server-side, so a tampered form can't inject a
    price into the catalog the agent quotes from."""
    _listing(tenant, price=4150)
    config.save_settings(tenant, units_json=json.dumps([
        {"id": "u1", "name": "Sunny House - Unit 1"},
    ]))
    # Unknown unit, unknown field, and a field that isn't enrichable.
    assert config.apply_enrichments(tenant, ["u9:monthly_price", "u1:template"], SITE) == 0
    config.apply_enrichments(tenant, ["u1:monthly_price"], SITE)
    assert config.get_units(tenant)[0]["monthly_price"] == 4150, "value comes from the listing"


def test_enrichment_never_overwrites_an_existing_value(tenant):
    _listing(tenant, price=4150)
    config.save_settings(tenant, units_json=json.dumps([
        {"id": "u1", "name": "Sunny House", "monthly_price": 1800},
    ]))
    config.apply_enrichments(tenant, ["u1:monthly_price"], SITE)
    assert config.get_units(tenant)[0]["monthly_price"] == 1800


def test_listing_photos_join_to_sub_units(tenant):
    """The listing is the parent ("Sunny House"); units are "Sunny House - Unit 1"."""
    _listing(tenant)
    config.save_settings(tenant, units_json=json.dumps([
        {"id": "u1", "name": "Sunny House - Unit 1"},
    ]))
    assert config.units_with_images(tenant, SITE)[0]["images"]
    # And an already-covered listing isn't offered again as a new property.
    assert config.discover_units(tenant, SITE) == []


# --- inbound email ingestion (a public, unauthenticated ingress) ------------


@pytest.fixture()
def inbox(monkeypatch):
    import importlib

    import inbound

    monkeypatch.setenv("INBOUND_EMAIL_DOMAIN", "inbound.example.com")
    monkeypatch.setenv("INBOUND_WEBHOOK_SECRET", "provider-secret")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    return importlib.reload(inbound)


FF_LEAD_EMAIL = """You have a new tenant lead.

Property: Quiet Spacious Home in NW DC - Unit 1
Traveler: Emma M.
Requested travel dates: Aug. 16, 2026 - Jul. 16, 2027
Travelers: 3
Traveling with pets: Yes
Budget: No Max
Reason for travel: Business Work
Date received: July 19, 2026

View this lead in your account.
"""


def _payload(inbound_mod, tenant_id="1", sender="no-reply@furnishedfinder.com",
             body=FF_LEAD_EMAIL, subject="New lead from Emma M."):
    return {
        "recipient": inbound_mod.address_for(tenant_id),
        "from": sender,
        "subject": subject,
        "text": body,
    }


def test_address_is_unguessable_and_verifies(inbox):
    addr = inbox.address_for("7")
    assert inbox.tenant_for_address(addr) == "7"
    # Tenant id alone isn't enough to forge one, and a bad token is rejected.
    assert inbox.tenant_for_address("leads+7@inbound.example.com") is None
    assert inbox.tenant_for_address("leads+7-" + "0" * 16 + "@inbound.example.com") is None
    # Knowing one tenant's address reveals nothing about another's.
    assert inbox.address_for("8") != addr


def test_forged_sender_is_rejected(inbox):
    for bad in ("someone@gmail.com", "a@evil-furnishedfinder.com", "", "furnishedfinder.com"):
        with pytest.raises(inbox.Rejected):
            inbox.accept(_payload(inbox, sender=bad), "provider-secret")


def test_wrong_or_missing_webhook_secret_is_rejected(inbox):
    with pytest.raises(inbox.Rejected):
        inbox.accept(_payload(inbox), "wrong-secret")
    with pytest.raises(inbox.Rejected):
        inbox.accept(_payload(inbox), "")


def test_webhook_fails_closed_when_unconfigured(inbox, monkeypatch):
    monkeypatch.delenv("INBOUND_WEBHOOK_SECRET", raising=False)
    assert not inbox.verify_webhook("anything")


def test_oversized_payload_is_rejected(inbox):
    with pytest.raises(inbox.Rejected):
        inbox.accept(_payload(inbox), "provider-secret",
                     raw_size=inbox.MAX_PAYLOAD_BYTES + 1)


def test_valid_lead_email_is_accepted_and_parsed(inbox):
    tenant_id, item = inbox.accept(_payload(inbox), "provider-secret")
    assert tenant_id == "1"
    assert item["kind"] == "lead"
    assert item["traveler"] == "Emma M."
    assert item["occupants"] == 3
    assert item["pets"] is True
    assert item["move_in"] == "8/16/26"
    assert item["move_out"] == "7/16/27"
    assert item["received_at"] == "July 19, 2026"
    assert item["source"] == "email", "lower-fidelity path must be marked"


def test_unparseable_mail_never_becomes_a_lead(inbox):
    for body, subject in [
        ("Your monthly FurnishedFinder newsletter is here.", "Newsletter"),
        ("", "New lead"),
        ("Nothing structured at all.", "Hello"),
    ]:
        with pytest.raises(inbox.Rejected):
            inbox.accept(_payload(inbox, body=body, subject=subject), "provider-secret")


def test_same_notification_twice_dedups(inbox, tenant):
    _tid, item = inbox.accept(_payload(inbox), "provider-secret")
    assert inbox.store(tenant, item, SITE) is True
    _tid, again = inbox.accept(_payload(inbox), "provider-secret")
    assert again["id"] == item["id"], "id must be stable across re-forwards"
    assert inbox.store(tenant, again, SITE) is False


def test_email_mode_is_never_scheduled_for_a_scrape(tenant):
    """The enforcement: email ingestion means no automated access to their site."""
    config.save_settings(tenant, autopilot="1", check_times="09:00",
                         ingest_mode=config.INGEST_EMAIL, last_check_at="")
    now = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)  # 10:00 New York, past the slot
    assert scheduler.due_slot(tenant, now) is None

    config.save_settings(tenant, ingest_mode=config.INGEST_BROWSER)
    assert scheduler.due_slot(tenant, now) is not None


def test_ingest_mode_defaults_to_email(tenant):
    config.save_settings(tenant, ingest_mode="")
    assert config.ingest_mode(tenant) == config.INGEST_EMAIL
    config.save_settings(tenant, ingest_mode="nonsense")
    assert config.ingest_mode(tenant) == config.INGEST_EMAIL


# --- email delivery ---------------------------------------------------------


def _reload_mailer(monkeypatch, **env):
    import importlib

    import mailer

    for key in ("RESEND_API_KEY", "RESEND_FROM", "SMTP_HOST", "SMTP_USER",
                "SMTP_PASS", "FROM_EMAIL", "FF_USERNAME"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(mailer)


def test_email_provider_selection(monkeypatch):
    m = _reload_mailer(monkeypatch)
    assert m.provider() == m.NONE and not m.is_configured()

    m = _reload_mailer(monkeypatch, SMTP_HOST="h", SMTP_USER="u@x.com", SMTP_PASS="p")
    assert m.provider() == m.SMTP

    # Resend wins when both are available — better auth and deliverability.
    m = _reload_mailer(monkeypatch, RESEND_API_KEY="re_x",
                       RESEND_FROM="Shorterm <hello@example.com>",
                       SMTP_HOST="h", SMTP_USER="u@x.com", SMTP_PASS="p")
    assert m.provider() == m.RESEND


def test_guest_email_never_spoofs_the_host_address(monkeypatch):
    """We must not put the host's own address in From: — we aren't authorised to
    send as their domain, so it fails SPF/DKIM and lands in spam (Resend rejects
    it outright). Their identity rides in the display name and Reply-To instead.
    """
    m = _reload_mailer(monkeypatch, RESEND_API_KEY="re_x",
                       RESEND_FROM="Shorterm <hello@example.com>")
    from_header, reply_to = m._sender("sagiv@gmail.com", "Sagiv")
    assert "gmail.com" not in from_header, "host address must not be the envelope sender"
    assert "hello@example.com" in from_header
    assert "Sagiv" in from_header
    assert reply_to == "sagiv@gmail.com", "replies must reach the host"


def test_system_email_has_no_reply_to(monkeypatch):
    m = _reload_mailer(monkeypatch, RESEND_API_KEY="re_x",
                       RESEND_FROM="Shorterm <hello@example.com>")
    from_header, reply_to = m._sender("", "")
    assert reply_to is None
    assert "hello@example.com" in from_header


def test_resend_payload_and_error_surfacing(monkeypatch):
    m = _reload_mailer(monkeypatch, RESEND_API_KEY="re_x",
                       RESEND_FROM="Shorterm <hello@example.com>")
    captured = {}

    class Ok:
        status_code = 200

        def json(self):
            return {"id": "x"}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, payload=json)
        return Ok()

    monkeypatch.setattr(m.requests, "post", fake_post)
    m.send_email("guest@example.com", "Subject", "Body",
                 from_email="sagiv@gmail.com", from_name="Sagiv")
    assert captured["url"] == m.RESEND_ENDPOINT
    assert captured["payload"]["to"] == ["guest@example.com"]
    assert captured["payload"]["reply_to"] == ["sagiv@gmail.com"]

    class Rejected:
        status_code = 403

        def json(self):
            return {"message": "domain is not verified"}

    monkeypatch.setattr(m.requests, "post", lambda *a, **k: Rejected())
    with pytest.raises(RuntimeError, match="not verified"):
        m.send_email("guest@example.com", "s", "b")


def test_unconfigured_email_raises_a_useful_error(monkeypatch):
    m = _reload_mailer(monkeypatch)
    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        m.send_email("g@example.com", "s", "b")


# --- account security -------------------------------------------------------


def test_weak_passwords_are_rejected(tenant):
    for bad in ("short", "password", "aaaaaaaaaaaa"):
        with pytest.raises(ValueError):
            models.validate_password(bad)
    models.validate_password("a-perfectly-fine-passphrase")


def test_account_locks_after_repeated_failures(tenant):
    email = "lockme@example.com"
    for _ in range(models.MAX_LOGIN_FAILURES):
        assert models.lockout_remaining(email) == 0
        models.record_login_failure(email)
    assert models.lockout_remaining(email) > 0
    models.clear_login_failures(email)
    assert models.lockout_remaining(email) == 0


def test_state_changing_posts_require_a_csrf_token(tenant, monkeypatch):
    """Without this, any page a logged-in host visits could make their browser
    send replies to guests or disconnect their FurnishedFinder account."""
    monkeypatch.setenv("INSECURE_COOKIES", "1")
    import dashboard

    dashboard.app.config["TESTING"] = True
    dashboard.app.config["WTF_CSRF_ENABLED"] = True
    client = dashboard.app.test_client()
    for path in ("/login", "/responder/send", "/refresh", "/disconnect", "/autopilot"):
        resp = client.post(path, data={"item_id": "x", "text": "hi"})
        assert resp.status_code == 400, f"{path} accepted a POST with no CSRF token"


def test_reset_token_is_single_use_and_tamper_proof(tenant):
    models.create_user("reset@example.com", "a-perfectly-fine-passphrase")
    token = models.make_reset_token("reset@example.com", "secret")
    assert models.verify_reset_token(token, "secret") == "reset@example.com"
    assert models.verify_reset_token(token + "x", "secret") is None
    assert models.verify_reset_token(token, "other-secret") is None
    # Using it invalidates it, because the hash it was signed against changed.
    models.set_password("reset@example.com", "another-good-passphrase")
    assert models.verify_reset_token(token, "secret") is None
