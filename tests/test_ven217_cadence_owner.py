"""VEN-217: exactly one owner advances the follow-up cadence after a send.

These drive the REAL `runner._send_worker` with only the browser seam stubbed.
That is the point: `tests/test_send_run_tokens.py` scripts `runner.send_reply`
away, so its harness cannot see the worker's own `after_contact` call and its
"advances exactly once" assertion passes on code that advances twice.
"""
import os
import tempfile
import time

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("FF_CRED_KEY", "c9jwUi0L-fUjf3wjbq74M0lK3ah7fmEfGhjxZ7RehQk=")
os.environ.setdefault("SECRET_KEY", "test-secret")

import config  # noqa: E402
import outbox  # noqa: E402
import pipeline  # noqa: E402
import storage  # noqa: E402

SITE = "furnishedfinder"


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ven217.db")
    monkeypatch.setattr(pipeline, "_TS_NORMALIZED", False, raising=False)
    tid = "1"
    config.save_settings(tid, host_name="Test Host", timezone="America/New_York",
                         reply_channels="platform")
    return tid


@pytest.fixture()
def seam(monkeypatch):
    """Stub ONLY the browser. Everything from `_send_worker` inward is real."""
    import contextlib

    import check_leads
    import runner
    from sites import furnishedfinder

    @contextlib.contextmanager
    def _page(tenant_id):
        yield object()

    monkeypatch.setattr(check_leads, "browser_page", _page)
    monkeypatch.setattr(furnishedfinder, "send_reply", lambda page, item, text: None)
    monkeypatch.setattr(furnishedfinder, "send_message_reply", lambda page, item, text: None)
    monkeypatch.setattr(furnishedfinder, "set_context", lambda *a, **k: None)
    monkeypatch.setattr(furnishedfinder, "clear_context", lambda *a, **k: None)
    with runner._lock:
        runner._state.update(running=False, status="idle", kind=None, tenant_id=None)
    yield
    with runner._lock:
        runner._state.update(running=False, status="idle", kind=None, tenant_id=None)


@pytest.fixture()
def contacts(monkeypatch):
    """Count `after_contact` calls while still letting the real one run."""
    import automation

    calls = []
    real = automation.after_contact

    def counting(tenant_id, site, item_id):
        calls.append(item_id)
        return real(tenant_id, site, item_id)

    monkeypatch.setattr(automation, "after_contact", counting)
    return calls


def _deal(tenant_id, item_id="s1", guest="Iris P."):
    item = {"id": item_id, "kind": "lead", "traveler": guest,
            "title": f"Unit | {guest}", "property_name": ""}
    storage.filter_new(tenant_id, SITE, "lead", [item])
    pipeline.ensure(tenant_id, SITE, item, None)
    storage.save_response(tenant_id, SITE, "lead", item_id,
                          draft="hello", status="draft")
    return item


def _settle(tenant, seconds=30):
    import runner

    deadline = time.time() + seconds
    while time.time() < deadline and runner.get_state(tenant).get("running"):
        time.sleep(0.1)
    time.sleep(0.3)


def test_a_drained_send_advances_the_cadence_exactly_one_step(tenant, seam, contacts):
    """`/responder/send`, `/outbox/<id>/approve`, autopilot and `worker.py` all
    land here: outbox -> drainer -> `send_next` -> `runner.send_reply`.

    Both `runner._send_worker` and `send_next` used to call `after_contact`, and
    it is not idempotent, so one delivered message advanced the deal two steps
    and the guest never received Followup 1.
    """
    import automation

    item = _deal(tenant)
    msg = outbox.add(tenant, SITE, item["id"], sequence="presale",
                     step_id="intro", step_label="First reply",
                     body="hello", auto=True)
    outbox.set_status(msg["id"], outbox.QUEUED)
    assert pipeline.get(tenant, SITE, item["id"]).get("step_index") == 0

    automation.send_next(tenant, SITE, timeout=30)
    _settle(tenant)

    assert outbox.get(msg["id"])["status"] == outbox.SENT
    deal = pipeline.get(tenant, SITE, item["id"])
    assert len(contacts) == 1, (
        "one delivered message is one contact; two calls advance the deal past "
        "a step the guest was never sent")
    assert deal["step_index"] == 1, "the deal owes Followup 1, not Followup 2"
    assert deal["next_action_step"] == "followup_1"
    # The second call also re-read a deal it had just moved to `contacted` and
    # promoted it again, so the board showed the guest a stage further on too.
    assert deal["stage"] == pipeline.CONTACTED


def test_the_direct_reply_route_still_advances_the_cadence(tenant, seam, contacts):
    """browser_server's `/v1/reply` calls `runner.send_reply` DIRECTLY — no
    outbox row and no `send_next`, so the worker's own call is the only thing
    that can start this guest's follow-up clock.

    This is the test that fails if the duplicate is resolved by deleting the
    worker's call instead: the guest is written to and the deal never enters the
    cadence at all.
    """
    import runner

    item = _deal(tenant, item_id="s2")
    assert pipeline.get(tenant, SITE, "s2").get("step_index") == 0

    runner.send_reply(tenant, SITE, item, "hello")
    _settle(tenant)

    deal = pipeline.get(tenant, SITE, "s2")
    assert len(contacts) == 1, "a reply that reached the guest must be recorded"
    assert deal["step_index"] == 1
    assert deal["next_action_step"] == "followup_1", (
        "the deal must be scheduled for its next touch")
    assert deal["stage"] == pipeline.CONTACTED
    assert deal["first_reply_at"], (
        "the response-time metric is stamped by `record_contact`")
