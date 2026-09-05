"""VEN-218: a send is only "sent" if a channel actually delivered it.

`runner._send_worker` guarded the *platform* reply with the channel check and
then ran everything below it unconditionally — the `status=sent` stamp, the
`first_reply_at` response-time metric and `automation.after_contact`, which
starts the follow-up cadence. The email send happened after all of that and its
failure was swallowed into a note, so the run still ended `done`.

For a tenant configured `reply_channels=email` the platform reply never
happens and the email is the only delivery. Measured on `0f0100e`, **four**
distinct arms delivered nothing and recorded a send anyway:

    email raises        -> "Reply sent to Dana Guest (email … FAILED — see logs)."
    SMTP not configured -> "Reply sent to Dana Guest (SMTP not configured — platform only)."
    no address on file  -> "Reply sent to Dana Guest (no email on file — platform only)."
    no sendable channel -> "Reply sent to Dana Guest."

All four: `response=sent`, `after_contact` called once, `stage=contacted`,
`step_index=1`, `first_reply_at` stamped. Only the first logs anything at all;
the other three are silent, and two of them tell an operator "platform only"
about a tenant who has the platform channel switched *off*. A fix scoped to
"the email raised" would have left three silent arms in place, so the guard is
"did any channel deliver", not "did the email throw".

**Two kinds of test live here and they are not the same evidence.**

*Reproductions* — the five `..._is_not_recorded_sent` / `..._records_the_outbox_row_failed`
tests. Each fails on `0f0100e` on a **value**, not on a missing symbol.

*Non-regression guards* — the three `..._still_...` / `..._keeps_...` tests.
These are **green on base by design**; they exist to stop the fix becoming a
new defect and their teeth are proved by mutation, not by a red baseline. Do
not "fix" them because they pass on both trees.

`test_platform_reply_keeps_email_best_effort` is the load-bearing one: email
stays best-effort whenever the platform reply landed, so the `platform,email`
majority with flaky SMTP is untouched by this change.

These drive the **real** `_send_worker`. Only the browser seam and the SMTP
seam are stubbed; `automation.after_contact` is wrapped in a counter that still
calls through, so the call count and the real lifecycle effect are observed in
the same run. Before this file, `git grep -l reply_channels tests/` returned
zero files — the whole channel branch was uncovered.
"""
import contextlib
import os
import tempfile

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("SECRET_KEY", "test-secret")

import config  # noqa: E402
import pipeline  # noqa: E402
import storage  # noqa: E402

SITE = "furnishedfinder"
ITEM_ID = "L-218"
GUEST = "Dana Guest"
GUEST_EMAIL = "guest@example.com"
ITEM = {"id": ITEM_ID, "kind": "lead", "traveler": GUEST,
        "received": "2026-09-01", "url": "https://example.invalid/lead/218"}


class Seams:
    """What each channel was asked to do, and what it did about it."""

    def __init__(self):
        self.platform_sends = []
        self.email_sends = []
        self.after_contact = []
        self.notifications = []


@pytest.fixture()
def seams(tmp_path, monkeypatch):
    """A tenant with a draft ready to send, and every I/O seam recorded.

    Stubs the browser and the mailer only. `after_contact` is counted *and*
    called through, so `deal["step_index"]` below is the lifecycle the real
    code produced rather than a restatement of the counter.
    """
    import automation
    import check_leads
    import mailer
    import runner
    from sites import furnishedfinder

    monkeypatch.setattr("db.DB_PATH", tmp_path / "ven218.db")
    monkeypatch.setattr(pipeline, "_TS_NORMALIZED", False, raising=False)

    tid = "1"
    config.seed_tenant(tid)
    s = Seams()

    @contextlib.contextmanager
    def _page(tenant_id):
        yield object()

    monkeypatch.setattr(check_leads, "browser_page", _page)

    def _send_reply(page, item, text):
        s.platform_sends.append(item["id"])

    monkeypatch.setattr(furnishedfinder, "send_reply", _send_reply)
    monkeypatch.setattr(furnishedfinder, "send_message_reply", _send_reply)
    monkeypatch.setattr(furnishedfinder, "set_context", lambda *a, **k: None)
    monkeypatch.setattr(furnishedfinder, "clear_context", lambda *a, **k: None)
    monkeypatch.setattr(runner, "notify",
                        lambda title, body="", **k: s.notifications.append((title, body)))

    def _send_email(to_addr, subject, body, from_email="", from_name=""):
        s.email_sends.append(to_addr)

    monkeypatch.setattr(mailer, "is_configured", lambda: True)
    monkeypatch.setattr(mailer, "send_email", _send_email)

    real_after_contact = automation.after_contact

    def _counting(tenant_id, site, item_id):
        s.after_contact.append(item_id)
        return real_after_contact(tenant_id, site, item_id)

    monkeypatch.setattr(automation, "after_contact", _counting)
    return s


def _seed(channels: str, tenant_email: str | None = GUEST_EMAIL) -> str:
    """A tenant on `channels` with a drafted reply waiting on a real deal."""
    tid = "1"
    config.save_settings(tid, reply_channels=channels, from_email="host@example.com",
                         host_name="Host Person")
    storage.filter_new(tid, SITE, "lead", [ITEM])
    storage.save_response(tid, SITE, "lead", ITEM_ID, status="draft", draft="hello",
                          tenant_email=tenant_email)
    pipeline.ensure(tid, SITE, ITEM)
    return tid


def _observe(tid: str) -> dict:
    """Every surface an operator can look at, after the worker has run."""
    import runner

    resp = storage.get_responses(tid, SITE).get(ITEM_ID) or {}
    deal = pipeline.get(tid, SITE, ITEM_ID) or {}
    state = runner.get_state(tid)
    return {
        "run_status": state.get("status"),
        "run_message": state.get("message"),
        "response_status": resp.get("status"),
        "sent_at": resp.get("sent_at"),
        "emailed_at": resp.get("emailed_at"),
        "stage": deal.get("stage"),
        "step_index": deal.get("step_index"),
        "next_action_step": deal.get("next_action_step"),
        "first_reply_at": deal.get("first_reply_at"),
    }


def _assert_nothing_recorded(obs: dict, seams: Seams) -> None:
    """No delivery happened, so no surface may claim one.

    Asserted by literal value rather than truthiness: on base every one of
    these holds the *opposite* concrete value, so the base failure is about
    what was recorded, not about a name that does not exist yet.
    """
    assert obs["run_status"] == "error", obs["run_message"]
    assert obs["response_status"] == "draft"
    assert obs["sent_at"] is None
    assert obs["emailed_at"] is None
    assert seams.after_contact == []
    assert obs["stage"] == pipeline.NEW
    assert obs["step_index"] == 0
    assert obs["next_action_step"] is None
    assert obs["first_reply_at"] is None


# ---------------------------------------------------------------------------
# Reproductions — RED on 0f0100e, one per zero-delivery arm.
# ---------------------------------------------------------------------------


def test_email_only_send_failure_is_not_recorded_sent(seams, monkeypatch):
    """AC1 — the filed arm. Email is the only channel and the send raises.

    On base: run `done`, response `sent`, cadence advanced to `followup_1`,
    and the only field telling the truth was the `emailed_at` NULL that no
    dashboard, template or test reads.
    """
    import mailer
    import runner

    def _boom(*a, **k):
        seams.email_sends.append(a[0])
        raise RuntimeError("smtp: 550 mailbox unavailable")

    monkeypatch.setattr(mailer, "send_email", _boom)

    tid = _seed("email")
    runner._send_worker(tid, SITE, ITEM, "Hi Dana, the unit is available.")

    obs = _observe(tid)
    assert seams.platform_sends == []
    assert seams.email_sends == [GUEST_EMAIL], "the email must still be attempted"
    _assert_nothing_recorded(obs, seams)
    assert GUEST_EMAIL in obs["run_message"]
    assert "only reply channel" in obs["run_message"]


def test_email_only_with_no_smtp_configured_is_not_recorded_sent(seams, monkeypatch):
    """AC2 — silent on base: zero send attempts, no log, no alert, and the
    operator was told "SMTP not configured — platform only" about a tenant
    with the platform channel switched off."""
    import mailer
    import runner

    monkeypatch.setattr(mailer, "is_configured", lambda: False)

    tid = _seed("email")
    runner._send_worker(tid, SITE, ITEM, "Hi Dana.")

    obs = _observe(tid)
    assert seams.email_sends == [], "nothing can be sent without SMTP"
    _assert_nothing_recorded(obs, seams)
    assert "not configured" in obs["run_message"]


def test_email_only_with_no_address_on_file_is_not_recorded_sent(seams):
    """AC3 — the responder never extracted an address, so there is nowhere to
    send. Silent on base, and reported as "platform only"."""
    import runner

    tid = _seed("email", tenant_email="")
    runner._send_worker(tid, SITE, ITEM, "Hi Dana.")

    obs = _observe(tid)
    assert seams.email_sends == []
    _assert_nothing_recorded(obs, seams)
    assert "no email address on file" in obs["run_message"]


def test_no_sendable_channel_is_not_recorded_sent(seams):
    """AC4 — the worst arm. `reply_channels=sms` enables nothing this app can
    send on, so both blocks are skipped entirely and base reported a clean,
    unqualified "Reply sent to Dana Guest." with zero attempts made.

    Reachable through a direct settings write or `REPLY_CHANNELS=sms` in the
    legacy onboarding env; the settings form cannot produce it, because
    unchecking every box saves `platform`.
    """
    import runner

    tid = _seed("sms")
    runner._send_worker(tid, SITE, ITEM, "Hi Dana.")

    obs = _observe(tid)
    assert seams.platform_sends == []
    assert seams.email_sends == []
    _assert_nothing_recorded(obs, seams)
    # Names the channel that *is* configured, so the operator can see why.
    assert "sms" in obs["run_message"]
    assert "Nothing was sent" in obs["run_message"]


def test_drained_email_only_failure_records_the_outbox_row_failed(seams, monkeypatch):
    """AC8 — the same failure through `automation.send_next`, the path that
    `/responder/send`, `/outbox/<id>/approve`, autopilot and `worker.py` all
    take.

    `send_next` branches on `runner.get_state().status == "error"`. On base the
    run ended `done`, so it wrote the outbox row `sent` with no error *and*
    called `after_contact` a second time — the guest who received nothing was
    filed mid-nurture at `step_index` 2, having skipped `followup_1`.

    The row must land `failed` with a legible error, because that is what puts
    it in `dashboard._failed_item_ids` behind a working "Retry send".
    """
    import automation
    import mailer
    import outbox
    import runner

    monkeypatch.setattr(automation, "_notify_failure", lambda *a, **k: None)

    def _boom(*a, **k):
        seams.email_sends.append(a[0])
        raise RuntimeError("smtp: 550 mailbox unavailable")

    monkeypatch.setattr(mailer, "send_email", _boom)

    tid = _seed("email")
    row = outbox.add(tid, SITE, ITEM_ID, sequence="presale", step_id="opener",
                     step_label="Opener", body="Hi Dana.", auto=True)
    assert row and row["status"] == outbox.QUEUED, "positive control: the row must enqueue"

    automation.send_next(tid, SITE, timeout=60)

    stored = outbox.get(row["id"])
    assert stored["status"] == outbox.FAILED
    assert "only reply channel" in (stored["error"] or "")
    _assert_nothing_recorded(_observe(tid), seams)


# ---------------------------------------------------------------------------
# Non-regression guards — GREEN on 0f0100e by design. Teeth proved by mutation.
# ---------------------------------------------------------------------------


def test_email_only_success_still_advances_the_cadence(seams):
    """AC5 — green on base. A working email-only tenant must be completely
    unaffected: the email *is* the delivery, so it starts the follow-up clock
    exactly as it did before.

    Teeth: dropping `delivered = True` after a successful email turns every
    healthy email-only tenant into a failed send. This is the test that catches
    that inversion.
    """
    import runner

    tid = _seed("email")
    runner._send_worker(tid, SITE, ITEM, "Hi Dana.")

    obs = _observe(tid)
    assert seams.email_sends == [GUEST_EMAIL]
    assert obs["run_status"] == "done"
    assert obs["response_status"] == "sent"
    assert obs["emailed_at"] is not None
    assert seams.after_contact == [ITEM_ID]
    assert obs["stage"] == pipeline.CONTACTED
    assert obs["step_index"] == 1
    assert obs["next_action_step"] == "followup_1"
    assert obs["first_reply_at"] is not None


def test_platform_reply_keeps_email_best_effort(seams, monkeypatch):
    """AC6 — green on base, and the guard that stops this fix becoming a worse
    defect than the one it closes.

    "Email is best effort" is correct whenever the platform reply landed; it is
    only wrong when email is the *only* channel. Failing the run on any email
    error would break every `platform,email` tenant with flaky SMTP — the
    majority — so the platform reply alone must still count as delivery.

    Teeth: dropping `delivered = True` after the platform reply makes this run
    fail, which is precisely the regression.
    """
    import mailer
    import runner

    def _boom(*a, **k):
        seams.email_sends.append(a[0])
        raise RuntimeError("smtp: 550 mailbox unavailable")

    monkeypatch.setattr(mailer, "send_email", _boom)

    tid = _seed("platform,email")
    runner._send_worker(tid, SITE, ITEM, "Hi Dana.")

    obs = _observe(tid)
    assert seams.platform_sends == [ITEM_ID]
    assert seams.email_sends == [GUEST_EMAIL]
    assert obs["run_status"] == "done"
    assert obs["response_status"] == "sent"
    assert obs["emailed_at"] is None, "the email genuinely did not go"
    assert seams.after_contact == [ITEM_ID]
    assert obs["stage"] == pipeline.CONTACTED
    assert obs["step_index"] == 1
    # The operator still hears about it, and the wording says the platform
    # reply landed — because on this arm it did.
    assert seams.notifications and "Platform reply" in seams.notifications[0][1]


def test_platform_failure_still_fails_the_run(seams, monkeypatch):
    """AC7 — green on base. The platform channel already raised on its own
    failures and that behaviour must not move: it is the arm the existing
    `except` was written for, and the fix must not change how it reports."""
    import runner
    from sites import furnishedfinder

    def _boom(page, item, text):
        seams.platform_sends.append(item["id"])
        raise RuntimeError("platform reply rejected")

    monkeypatch.setattr(furnishedfinder, "send_reply", _boom)

    tid = _seed("platform,email")
    runner._send_worker(tid, SITE, ITEM, "Hi Dana.")

    obs = _observe(tid)
    assert seams.platform_sends == [ITEM_ID]
    assert seams.email_sends == [], "the email block is never reached"
    _assert_nothing_recorded(obs, seams)
    assert obs["run_message"] == "Send failed: platform reply rejected"
