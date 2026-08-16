"""The release guard must refuse only sends that are actually going out.

Round 5 accepted the guard's race fix and rejected its blast radius. The guard
tested `status IN (queued, sending)`, but `outbox.next_queued` — the only thing
that ever *delivers* a row — additionally gates on `scheduled_at <= now`. Two
definitions of "in flight" inside one module, and the broader one won:

* a `queued` row scheduled past its time is not deliverable by anything, yet it
  refused approve/retry/send for that guest for the entire window. Reached the
  ordinary way, not exotically: `automation.enqueue_autopilot_reply` schedules
  through `sequences.next_send_time()`, i.e. the quiet-hours clamp, so an
  evening inquiry under shipped defaults locked the guest until morning.
* and the row doing the blocking was rendered **nowhere** — `_board` built its
  approval list from `PENDING` only — so the operator could neither see the
  blocker nor clear it without hand-POSTing `/outbox/<id>/cancel`.

Both halves are needed and the tests below separate them, because the second is
what covers the variant a `scheduled_at` gate cannot: a *due* `queued` row that
no drainer ever picks up (worker down, no in-process browser). That one blocks
correctly and indefinitely, so the escape has to be an affordance, not a clock.

Guarding the inverse is half the file. A gate that refuses nothing is not a fix
— `test_*_still_blocks_*` and `test_missing_scheduled_at_counts_as_due` fail if
the predicate is loosened past what `next_queued` would actually deliver.
"""
import os
import re
import tempfile
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("FF_CRED_KEY", "c9jwUi0L-fUjf3wjbq74M0lK3ah7fmEfGhjxZ7RehQk=")
os.environ.setdefault("SECRET_KEY", "test-secret")

import config  # noqa: E402
import ff_account  # noqa: E402
import outbox  # noqa: E402
import pipeline  # noqa: E402
import storage  # noqa: E402
import timeframe  # noqa: E402

SITE = "furnishedfinder"
EMAIL = "host@example.com"
PASSWORD = "a-perfectly-fine-passphrase"

SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.S | re.I)


def _shift(hours):
    """An absolute schedule stamp `hours` from now, in the schedule frame."""
    base = datetime.fromisoformat(timeframe.now())
    return (base + timedelta(hours=hours)).isoformat(timespec="seconds")


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    import db
    import models

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "duegate.db")
    monkeypatch.setattr(pipeline, "_TS_NORMALIZED", False, raising=False)
    monkeypatch.setenv("INSECURE_COOKIES", "1")

    user = models.create_user(EMAIL, PASSWORD)
    tid = str(user.tenant_id)
    config.save_settings(tid, host_name="Test Host", timezone="America/New_York",
                         onboarded="1")
    # Without a connected account the template renders the verification note
    # instead of the board, which would pass every "not rendered" assertion.
    ff_account.connect(tid, "ff@example.com")
    ff_account.mark_state(tid, "connected")
    return tid


@pytest.fixture()
def client(tenant, monkeypatch):
    import automation
    import dashboard

    # The read path starts a drainer, which would deliver the fixture rows out
    # from under the assertions.
    monkeypatch.setattr(automation, "start_drainer", lambda *a, **k: None)
    dashboard.app.config["TESTING"] = True
    dashboard.app.config["WTF_CSRF_ENABLED"] = False
    c = dashboard.app.test_client()
    resp = c.post("/login", data={"email": EMAIL, "password": PASSWORD})
    assert resp.status_code == 302, f"login did not authenticate: {resp.status_code}"
    return c


def _deal(tenant_id, item_id, *, guest="Dana R."):
    item = {"id": item_id, "kind": "lead", "traveler": guest,
            "title": f"Unit | {guest}", "property_name": ""}
    storage.filter_new(tenant_id, SITE, "lead", [item])
    pipeline.ensure(tenant_id, SITE, item, None)
    storage.save_response(tenant_id, SITE, "lead", item_id,
                          status="draft", draft="Hi Dana, yes it's free.",
                          reason="good fit", confidence="high")
    return item


def _add(tenant_id, item_id, *, auto, scheduled_at=None, body="hello"):
    return outbox.add(tenant_id, SITE, item_id, sequence="presale",
                      step_id="intro", step_label="First reply",
                      body=body, auto=auto, scheduled_at=scheduled_at)


def _statuses(tenant_id, item_id):
    """Row statuses read raw, so a base-commit run fails on behaviour not API."""
    with outbox._conn() as c:
        return [tuple(r) for r in c.execute(
            "SELECT id, status FROM outbox WHERE tenant_id=? AND site=? "
            "AND item_id=? ORDER BY id ASC",
            (str(tenant_id), SITE, str(item_id))).fetchall()]


# --------------------------------------------------------------------------
# 1. The lockout: a deferred sibling must not refuse the operator
# --------------------------------------------------------------------------

def test_a_deferred_queued_sibling_does_not_block_approval(tenant):
    """The reviewer's A/B, as a test. Base: refused. Fixed: released."""
    _deal(tenant, "L1")
    blocker = _add(tenant, "L1", auto=True, scheduled_at=_shift(8))
    pending = _add(tenant, "L1", auto=False)

    # Precondition — the thing that makes this a lockout and not a race: the
    # blocking row is not deliverable by the only code path that delivers.
    assert outbox.next_queued(tenant) is None, (
        "precondition failed: the deferred row is due, so refusing the operator "
        "would be correct and this test would prove nothing")

    released, row = outbox.release_to_send(pending["id"],
                                           from_statuses=outbox.APPROVABLE)
    assert released is True, (
        "a row nothing can deliver for 8h blocked the operator's approval "
        f"(rows now {_statuses(tenant, 'L1')})")
    assert row["status"] == outbox.QUEUED


def test_a_deferred_queued_sibling_does_not_block_a_new_send(tenant):
    """Same gate, insert-shaped half — `add(unless_in_flight=True)`."""
    _deal(tenant, "L1")
    _add(tenant, "L1", auto=True, scheduled_at=_shift(8))
    assert outbox.next_queued(tenant) is None

    wrote = outbox.add(tenant, SITE, "L1", sequence="presale", step_id="manual",
                       step_label="Manual reply", body="second", auto=True,
                       unless_in_flight=True)
    assert wrote is not None, (
        "the send button refused over a row deferred 8h into the future")


# --------------------------------------------------------------------------
# 2. The inverse: what is genuinely going out must still refuse
# --------------------------------------------------------------------------

def test_a_due_queued_sibling_still_blocks_approval(tenant):
    """The guard's whole purpose. Loosening the gate must not reach this."""
    _deal(tenant, "L1")
    _add(tenant, "L1", auto=True)          # scheduled_at defaults to now
    pending = _add(tenant, "L1", auto=False)

    assert outbox.next_queued(tenant) is not None, (
        "precondition failed: the blocker is not actually deliverable")

    released, _ = outbox.release_to_send(pending["id"],
                                         from_statuses=outbox.APPROVABLE)
    assert released is False, (
        "approval released a second message while a due row was queued for the "
        f"same guest (rows now {_statuses(tenant, 'L1')})")


def test_a_sending_sibling_blocks_however_it_was_scheduled(tenant):
    """`sending` is not gated on the stamp — a drainer already holds that row.

    A future `scheduled_at` on a row a browser is mid-delivery on is history,
    not a promise. Gating `sending` too would have re-opened the double-send
    this guard exists to close, against the exact rows most likely to hit it.
    """
    _deal(tenant, "L1")
    blocker = _add(tenant, "L1", auto=True, scheduled_at=_shift(8))
    outbox.set_status(blocker["id"], outbox.SENDING)
    pending = _add(tenant, "L1", auto=False)

    released, _ = outbox.release_to_send(pending["id"],
                                         from_statuses=outbox.APPROVABLE)
    assert released is False, (
        "approval released a second message into a thread a browser was "
        f"already delivering (rows now {_statuses(tenant, 'L1')})")


def test_missing_scheduled_at_counts_as_due(tenant):
    """Fail-safe: an unreadable stamp is not evidence that nothing is going out.

    Legacy rows predate the column being written unconditionally. `NULL <= now`
    is NULL in SQL — i.e. not true — so a naive gate would silently stop
    treating exactly those rows as blockers.
    """
    _deal(tenant, "L1")
    blocker = _add(tenant, "L1", auto=True)
    with outbox._conn() as c:
        c.execute("UPDATE outbox SET scheduled_at=NULL WHERE id=?",
                  (blocker["id"],))
    assert outbox.get(blocker["id"])["scheduled_at"] is None, (
        "precondition failed: the stamp was not actually cleared")

    pending = _add(tenant, "L1", auto=False)
    released, _ = outbox.release_to_send(pending["id"],
                                         from_statuses=outbox.APPROVABLE)
    assert released is False, (
        "a queued row with no schedule stamp stopped blocking, so a legacy row "
        "silently became invisible to the double-send guard")


# --------------------------------------------------------------------------
# 3. One predicate, three callers — the drift this ticket is about
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status,offset,expected", [
    (outbox.QUEUED, -1, True),    # due
    (outbox.QUEUED, 8, False),    # deferred
    (outbox.SENDING, -1, True),
    (outbox.SENDING, 8, True),    # a held row ignores its stamp
    (outbox.PENDING, -1, False),
    (outbox.SENT, -1, False),
    (outbox.FAILED, -1, False),
    (outbox.CANCELED, -1, False),
])
def test_sql_and_python_in_flight_agree(tenant, status, offset, expected):
    """`_row_in_flight` is a hand-written twin of `_in_flight_terms`.

    Two implementations of one rule is how this module got here in the first
    place, so they are asserted equal row-for-row rather than assumed to be.
    `in_flight_for_item` is the SQL side's observable form.
    """
    _deal(tenant, "L1")
    msg = _add(tenant, "L1", auto=status != outbox.PENDING,
               scheduled_at=_shift(offset))
    if status not in (outbox.PENDING, outbox.QUEUED):
        outbox.set_status(msg["id"], status)
    row = outbox.get(msg["id"])
    assert row["status"] == status, "precondition: row not in the tested status"

    sql_side = outbox.in_flight_for_item(tenant, SITE, "L1") is not None
    py_side = outbox._row_in_flight(row, timeframe.now())
    assert sql_side == py_side == expected, (
        f"{status} at {offset:+}h: SQL said {sql_side}, Python said {py_side}, "
        f"expected {expected}")


# --------------------------------------------------------------------------
# 4. The caption: "Queued to send…" eight hours early is the ticket's own bug
# --------------------------------------------------------------------------

def test_send_state_does_not_call_a_deferred_row_in_flight(tenant):
    _deal(tenant, "L1")
    _add(tenant, "L1", auto=True, scheduled_at=_shift(8))
    state = outbox.send_state(outbox.rows_by_item(tenant, SITE).get("L1"))

    assert state["in_flight"] is False, (
        "the card disabled its send button over a row that cannot send for 8h")
    assert state["label"] != "Queued to send…", (
        "the caption still claims an imminent send 8h early — the same "
        "misrepresentation this ticket was filed about")


def test_send_state_still_reports_a_due_row_as_in_flight(tenant):
    _deal(tenant, "L1")
    _add(tenant, "L1", auto=True)
    state = outbox.send_state(outbox.rows_by_item(tenant, SITE).get("L1"))
    assert state["in_flight"] is True and state["label"] == "Queued to send…"


def test_send_state_agrees_with_the_guard_it_renders(tenant):
    """The button's disabled state and the server's refusal, from one rule.

    VEN-131 is exactly this pair disagreeing. Asserted over both sides of the
    gate so neither can drift alone.
    """
    for item_id, offset in (("L1", 8), ("L2", -1)):
        _deal(tenant, item_id)
        _add(tenant, item_id, auto=True, scheduled_at=_shift(offset))
        pending = _add(tenant, item_id, auto=False)
        state = outbox.send_state(outbox.rows_by_item(tenant, SITE).get(item_id))
        released, _ = outbox.release_to_send(pending["id"],
                                             from_statuses=outbox.APPROVABLE)
        assert state["in_flight"] is not released, (
            f"{item_id} (offset {offset:+}h): card said in_flight="
            f"{state['in_flight']} while the server "
            f"{'accepted' if released else 'refused'} the release")


# --------------------------------------------------------------------------
# 5. The blocker has to be visible, and clearable — the part a clock can't fix
# --------------------------------------------------------------------------

def test_a_blocking_queued_row_is_rendered_with_a_cancel_control(client, tenant):
    """The due-now/no-drainer variant: blocks correctly, forever, so it needs
    an affordance rather than a gate. Base rendered this row nowhere."""
    _deal(tenant, "L1")
    blocker = _add(tenant, "L1", auto=True, body="the blocking message")
    assert outbox.next_queued(tenant) is not None, (
        "precondition: this variant is supposed to be genuinely deliverable")

    html = SCRIPT_RE.sub("", client.get("/dashboard").get_data(as_text=True))

    assert "the blocking message" in html, (
        "the row that refuses approve/retry/send for this guest is rendered "
        "nowhere on the page")
    assert re.search(r"cancelMsg\(%d\)" % blocker["id"], html), (
        "no cancel control for the blocking row, so the only escape from the "
        "refusal is hand-POSTing /outbox/<id>/cancel")


def test_cancelling_the_blocker_immediately_unblocks_the_guest(client, tenant):
    """End to end through the real route, which is the operator's actual escape."""
    _deal(tenant, "L1")
    blocker = _add(tenant, "L1", auto=True)
    pending = _add(tenant, "L1", auto=False)

    released, _ = outbox.release_to_send(pending["id"],
                                         from_statuses=outbox.APPROVABLE)
    assert released is False, "precondition: the guest is not actually blocked"

    resp = client.post(f"/outbox/{blocker['id']}/cancel", data={})
    assert resp.status_code == 200, f"cancel returned {resp.status_code}"
    assert outbox.get(blocker["id"])["status"] == outbox.CANCELED

    released, _ = outbox.release_to_send(pending["id"],
                                         from_statuses=outbox.APPROVABLE)
    assert released is True, (
        "cancelling the blocker did not restore the operator's ability to act "
        f"(rows now {_statuses(tenant, 'L1')})")
