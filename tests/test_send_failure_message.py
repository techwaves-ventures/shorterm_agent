"""VEN-220 — a failed send is labelled once, by the surface that shows it.

The board rendered every failed send as `Send failed: Send failed: reply box not
found`. Two layers each added the label: `runner._send_worker`'s `except` stored
`f"Send failed: {e}"`, `automation.send_next` copies that message verbatim into
`outbox.error`, and `dashboard.html` prepends "Send failed: " again.

The card is not the only reader, which is why these tests drive the real
`automation.send_next` and then assert on the *stored column* and on the two
other surfaces built from it:

  * `outbox.error`                      — the column every reader shares
  * `digest.build`                      — prints it under "** N messages FAILED to send **"
  * `automation._notify_failure`         — "… didn't go out: {reason}"

All three doubled the label before the fix. The fourth reader — the run banner —
renders the message with no framing of its own and is covered by the browser
pass on the PR rather than here.

Every assertion below is on a literal string a host reads, not on a count or a
`in`-check that a differently-broken message would still satisfy.
"""
import contextlib
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

SITE = "furnishedfinder"

# Namespaced: `db.DB_PATH` is resolved once per interpreter, so a whole-suite run
# puts every module's fixtures in one file and an id shared with a sibling turns
# *its* tests red.
ITEM_ID = "ven220-lead"
GUEST = "Dana Guest"


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ven220.db")
    monkeypatch.setattr(pipeline, "_TS_NORMALIZED", False, raising=False)
    tid = "1"
    config.save_settings(tid, host_name="Test Host", timezone="America/New_York",
                         reply_channels="platform")
    return tid


@pytest.fixture()
def failed_send(tenant, monkeypatch):
    """Drive one real send to failure and hand back what every surface reads.

    The failure is injected at the browser, below `_send_worker`'s `try`, so the
    reason travels the production path: worker `except` → run state → the
    `status == "error"` branch of `automation.send_next` → `outbox.error`.
    """
    def _run(exc: BaseException = RuntimeError("reply box not found")):
        import automation
        import check_leads
        import notify as notify_mod
        import runner

        item = {"id": ITEM_ID, "kind": "lead", "traveler": GUEST,
                "title": f"Unit | {GUEST}", "property_name": ""}
        storage.filter_new(tenant, SITE, "lead", [item])
        pipeline.ensure(tenant, SITE, item, None)
        msg = outbox.add(tenant, SITE, ITEM_ID, sequence="presale", step_id="intro",
                         step_label="Followup 1", body="hello", auto=True)
        outbox.set_status(msg["id"], outbox.QUEUED)

        @contextlib.contextmanager
        def _raises(tenant_id):
            raise exc
            yield  # pragma: no cover

        monkeypatch.setattr(check_leads, "browser_page", _raises)

        notifications = []
        monkeypatch.setattr(notify_mod, "notify",
                            lambda title, body, **kw: notifications.append((title, body)))

        automation.send_next(tenant, SITE, timeout=30)

        row = outbox.get(msg["id"])
        assert row["status"] == outbox.FAILED, "precondition: the send must have failed"
        return {"row": row, "error": row.get("error"),
                "notifications": notifications, "state": runner.get_state(tenant)}

    return _run


def test_the_stored_error_is_the_bare_reason(failed_send):
    """`outbox.error` holds a reason, not a sentence.

    Four other writers of this column already store one — `"stored item not
    found"`, `"timed out waiting for the send to finish"`, the
    abandon-after-N-attempts note — so the readers were built to add the framing.
    This handler was the only writer that also supplied a label.
    """
    result = failed_send()
    assert result["error"] == "reply box not found", (
        "the column readers each prepend their own label; a labelled value here "
        "is rendered twice"
    )


def test_no_surface_says_send_failed_twice(failed_send):
    """The filed defect, stated as the card renders it.

    `dashboard.html` builds the card message as `"Send failed: " + s.error`, so
    a stored value carrying the same label is what a host actually reads.
    """
    result = failed_send()
    card = "Send failed: " + (result["error"] or "unknown error")
    assert card == "Send failed: reply box not found", (
        f"the board reads {card!r}"
    )


def test_the_digest_names_the_failure_once(tenant, failed_send):
    """The end-of-day email is the surface a host reads when logged out."""
    import digest

    failed_send()
    content = digest.build(tenant)
    assert content is not None, "a failed message must produce a digest"
    body = content["body"]
    assert "** 1 message FAILED to send **" in body, "precondition: the failure section"
    assert f"  - {GUEST}: reply box not found" in body, (
        f"digest body was:\n{body}"
    )


def test_the_failure_notification_names_the_failure_once(failed_send):
    """`_notify_failure` writes its own sentence around the reason."""
    result = failed_send()
    bodies = [b for t, b in result["notifications"] if t == "Reply failed to send"]
    assert bodies == [
        "Followup 1 for this guest didn't go out: reply box not found"
    ], f"notifications were {result['notifications']!r}"


def test_an_exception_carrying_no_message_still_names_something(failed_send):
    """`str(e)` is empty for `raise SomeError()`, and a blank error names nothing.

    The old label accidentally guaranteed the column was never empty; dropping it
    has to keep that guarantee, or the card degrades to "Send failed: unknown
    error" and the banner to a bare status word.
    """
    result = failed_send(RuntimeError())
    assert result["error"] == "RuntimeError", (
        "an exception with no message must still identify itself"
    )


def test_the_card_template_still_supplies_the_label():
    """Non-regression guard: green before and after this change, on purpose.

    The label has to live in exactly one layer, and this is the layer that keeps
    it — the card is the only framing the four *other* writers of `outbox.error`
    ever get. Removing it here (the obvious symmetrical way to close VEN-220)
    would leave "stored item not found" rendering as a bare fragment under a
    guest's name. Pinned as source text because the branch is JavaScript.
    """
    from pathlib import Path

    template = Path(__file__).resolve().parents[1] / "templates" / "dashboard.html"
    html = template.read_text(encoding="utf-8")
    assert '"Send failed: " + (s.error || "unknown error")' in html, (
        "the failed-card branch must keep its label; the stored column is a bare "
        "reason and nothing else labels it"
    )
