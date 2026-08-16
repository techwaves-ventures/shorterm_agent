"""One message per guest per exchange, and a refusal the operator can undo.

Round 5 accepted the guard's race fix and rejected its blast radius: a `queued`
row deferred by the quiet-hours clamp refused approve/retry/send for that guest
for the whole window, and the row doing the blocking was rendered **nowhere**,
so the only escape was hand-POSTing `/outbox/<id>/cancel`.

Round 6 tried to fix that by narrowing the guard to `scheduled_at <= now`, on
the reasoning that a row no drainer will touch for eight hours is not "in
flight". **That was wrong, and this file used to assert it.** A deferred row is
not cancelled, it is *scheduled*: releasing a second message beside it does not
avoid a collision, it guarantees one. Measured through the real routes, it put
two messages in front of one guest where the broad test put one — and on the
`/responder/send` path the two bodies were byte-identical, because `runner.py`
hands the same draft string to `enqueue_autopilot_reply` and to the textarea the
operator posts.

Serializing at drain time does not rescue the narrow gate either: spacing the
two rows apart still delivers both, and nothing downstream compares bodies
(`sent_bodies()` is read only by `/responder/send`'s pre-read, never by
`send_next`). So the guard keeps the broad test, and the lockout is cured by the
*affordance* instead — the blocking row is rendered with a working "Don't send".
That also covers the variant no clock could: a *due* `queued` row that no
drainer ever picks up (worker down, no in-process browser) blocks correctly and
indefinitely.

Two properties this file exists to keep apart, because fusing them is what broke
it: whether a row **blocks** (`_row_in_flight`, status membership) and how a row
is **labelled** (`_row_deferred`, which reads the stamp). A deferred row blocks
*and* says "Scheduled to send".
"""
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

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

def test_only_one_message_is_on_its_way_after_the_operator_acts(tenant):
    """The assertion round 6 never made, and the one that catches its defect.

    Counted as *deliveries*, which is the property the guest experiences. Round
    6 asserted only that the operator was not refused, so it read as fixed while
    stacking a second message behind the first. Both doors are driven here: the
    UPDATE-shaped release and the INSERT-shaped `/responder/send`.
    """
    _deal(tenant, "L1")
    # Exactly what the quiet-hours clamp produces for an evening inquiry.
    _add(tenant, "L1", auto=True, scheduled_at=_shift(8))
    pending = _add(tenant, "L1", auto=False)

    outbox.release_to_send(pending["id"], from_statuses=outbox.APPROVABLE)
    outbox.add(tenant, SITE, "L1", sequence="presale", step_id="manual",
               step_label="Manual reply", body="second", auto=True,
               unless_in_flight=True)

    undelivered = [s for _, s in _statuses(tenant, "L1")
                   if s in (outbox.QUEUED, outbox.SENDING)]
    assert len(undelivered) == 1, (
        "more than one message is on its way to this guest "
        f"(rows now {_statuses(tenant, 'L1')}). A deferred row is not cancelled, "
        "it is scheduled: releasing a second beside it does not avoid a "
        "collision, it guarantees one.")


def test_a_deferred_queued_sibling_still_blocks_approval(tenant):
    """Deferred is not harmless — it is the *ordinary* way to reach the defect.

    `automation.enqueue_autopilot_reply` schedules through
    `sequences.next_send_time()`, i.e. the quiet-hours clamp, so an evening
    inquiry under shipped defaults produces exactly this row. The operator is
    not locked out by it: the escape is the "Don't send" control in section 5,
    not a guard that lets a second message through.
    """
    _deal(tenant, "L1")
    _add(tenant, "L1", auto=True, scheduled_at=_shift(8))
    pending = _add(tenant, "L1", auto=False)

    # The blocker is genuinely not deliverable *yet* — which is precisely why
    # narrowing the guard to "deliverable now" looked reasonable and was not.
    assert outbox.next_queued(tenant) is None, (
        "precondition failed: the deferred row is already due")

    released, _ = outbox.release_to_send(pending["id"],
                                         from_statuses=outbox.APPROVABLE)
    assert released is False, (
        "approval released a second message beside a row that will be delivered "
        f"in 8h (rows now {_statuses(tenant, 'L1')})")


def test_a_deferred_queued_sibling_still_blocks_a_new_send(tenant):
    """Same rule, insert-shaped half — `add(unless_in_flight=True)`.

    This is the `/responder/send` door, and the one where the duplicate is
    literally the same text twice: `runner.py` gives the autopilot row and the
    operator's textarea the same draft string.
    """
    _deal(tenant, "L1")
    _add(tenant, "L1", auto=True, scheduled_at=_shift(8))

    wrote = outbox.add(tenant, SITE, "L1", sequence="presale", step_id="manual",
                       step_label="Manual reply", body="second", auto=True,
                       unless_in_flight=True)
    assert wrote is None, (
        "the send button queued a second message beside a deferred row; both "
        f"come due and both are delivered (rows now {_statuses(tenant, 'L1')})")


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


def test_a_queued_row_with_no_schedule_stamp_still_blocks(tenant):
    """Fail-safe: an unreadable stamp is not evidence that nothing is going out.

    A stampless row is not reachable through the product — `add()` has written
    `scheduled_at or now` since the column existed, and it was never added by
    migration, so the "legacy rows" story an earlier round told here was simply
    false. Kept anyway, and manufactured by raw SQL, because it pins the safe
    direction of the fail: any predicate that starts reading the stamp again
    must not let a row it cannot parse slip past the guard.
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
    (outbox.QUEUED, 8, True),     # deferred — still going to be delivered
    (outbox.SENDING, -1, True),
    (outbox.SENDING, 8, True),
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

    The `offset` column is what makes this table load-bearing now: both `queued`
    rows expect True, so any predicate that starts consulting the stamp to
    decide *blocking* fails here regardless of which direction it leans.
    """
    _deal(tenant, "L1")
    msg = _add(tenant, "L1", auto=status != outbox.PENDING,
               scheduled_at=_shift(offset))
    if status not in (outbox.PENDING, outbox.QUEUED):
        outbox.set_status(msg["id"], status)
    row = outbox.get(msg["id"])
    assert row["status"] == status, "precondition: row not in the tested status"

    sql_side = outbox.in_flight_for_item(tenant, SITE, "L1") is not None
    py_side = outbox._row_in_flight(row)
    assert sql_side == py_side == expected, (
        f"{status} at {offset:+}h: SQL said {sql_side}, Python said {py_side}, "
        f"expected {expected}")


# --------------------------------------------------------------------------
# 4. The caption: "Queued to send…" eight hours early is the ticket's own bug
# --------------------------------------------------------------------------

def test_a_deferred_row_is_labelled_honestly_but_still_blocks(tenant):
    """The two properties, asserted together so neither can be traded away.

    Round 6 read the caption complaint as licence to weaken the guard. It was
    only ever a complaint about *wording*: the control stays disabled, because
    the row really is going to be sent — it just stops claiming that is
    imminent, and carries the time so the operator can check.
    """
    _deal(tenant, "L1")
    _add(tenant, "L1", auto=True, scheduled_at=_shift(8))
    state = outbox.send_state(outbox.rows_by_item(tenant, SITE).get("L1"))

    assert state["in_flight"] is True, (
        "a deferred row stopped blocking; a second message can now be released "
        "beside it and the guest receives both")
    assert state["label"] != "Queued to send…", (
        "the caption still claims an imminent send 8h early")
    assert state["scheduled_at"], (
        "'Scheduled to send' without the time is not checkable by the operator")


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


def test_cancel_is_a_compare_and_set_not_a_read_then_write(tenant, monkeypatch):
    """The window putting "Don't send" on `queued` rows opened.

    Harmless while that button only sat on `pending_approval` rows, which no
    drainer can claim — `next_queued` selects `queued`. On a `queued` row the
    drainer is the other party, and the claim can land between the read and the
    write. Raced for real, 39 of 40 trials ended wrong, so the claim is injected
    at exactly that point here rather than left to chance.

    Losing the race must leave the row alone. Overwriting a live `sending` row
    is the worse half: it lands `canceled` with `sending_at` set, so the guest
    is written to *and* the row drops out of `sent_bodies()` — the one
    duplicate-send guard `/responder/send` has.
    """
    _deal(tenant, "L1")
    msg = _add(tenant, "L1", auto=True)

    real_get = outbox.get
    claimed = {"done": False}

    def racing_get(mid):
        row = real_get(mid)
        if not claimed["done"]:          # the drainer, immediately after the read
            claimed["done"] = True
            outbox.set_status(mid, outbox.SENDING)
        return row

    monkeypatch.setattr(outbox, "get", racing_get)
    outbox.cancel(msg["id"])
    # Restore just this attribute. `monkeypatch.undo()` would also revert the
    # `db.DB_PATH` the `tenant` fixture set, pointing the assertion below at a
    # different database — which reads as the row vanishing.
    monkeypatch.setattr(outbox, "get", real_get)

    assert outbox.get(msg["id"])["status"] == outbox.SENDING, (
        "cancel overwrote a row a drainer had already claimed; the guest still "
        "receives it, and it is no longer in sent_bodies() to stop a second copy")


def test_the_drainer_does_not_reclaim_a_row_cancelled_under_it(tenant, monkeypatch):
    """The other half of the same race, from the drainer's side.

    `send_next` reads with `next_queued` and then claims. The claim used to be
    an unconditional write, so a cancel landing in that gap was silently undone
    and the message went to the guest anyway — the comment above it has always
    said "so a second drainer can't pick up the same row", which was simply not
    what the code did.

    Written because the CAS was otherwise asserted nowhere: reverting it to the
    bare write left the whole suite green, which is the failure mode this ticket
    has already shipped twice.
    """
    import automation
    import runner

    _deal(tenant, "L1")
    msg = _add(tenant, "L1", auto=True)

    dispatched = []
    monkeypatch.setattr(runner, "send_reply",
                        lambda *a, **k: (dispatched.append(a), {"status": "ok"})[1])

    real_next = outbox.next_queued

    def racing_next(*a, **k):
        row = real_next(*a, **k)
        if row:                       # the operator cancels, post-read
            outbox.set_status(row["id"], outbox.CANCELED)
        return row

    monkeypatch.setattr(outbox, "next_queued", racing_next)
    automation.send_next(tenant, SITE)

    assert dispatched == [], (
        "the drainer dispatched a message the operator had already cancelled")
    assert outbox.get(msg["id"])["status"] == outbox.CANCELED, (
        "the claim overwrote a cancelled row, so the cancel was silently undone "
        f"(row now {outbox.get(msg['id'])['status']})")


def test_the_cancel_route_reports_the_race_rather_than_a_green_toast(client, tenant):
    """`{"ok": true}` over a message that ships anyway is the lie to avoid."""
    _deal(tenant, "L1")
    msg = _add(tenant, "L1", auto=True)
    outbox.set_status(msg["id"], outbox.SENDING)

    resp = client.post(f"/outbox/{msg['id']}/cancel", data={})
    assert resp.status_code == 409, (
        f"cancel answered {resp.status_code} for a row already being delivered")
    assert outbox.get(msg["id"])["status"] == outbox.SENDING


# --------------------------------------------------------------------------
# 6. Which of an item's rows gets to speak for it
#
# Round 7 ranked `sending` over `queued` and stopped there, breaking the tie
# among the rest on id. Id order is unrelated to send order, so for rows
# [deferred, due] the card was captioned by the message leaving tomorrow while
# its sibling left on the drainer's next pass: "Scheduled to send" — *there is
# still time to stop this* — over a send with seconds to live. That is this
# ticket's own failure class, and the suite was green with it present.
#
# So these run each case in **both insertion orders**. A rule that reads
# correctly in one order and not the other is not a rule, and asserting only
# the order that happens to pass is how the first version shipped.
# --------------------------------------------------------------------------

def _governing(tenant_id, item_id):
    return outbox.send_state(outbox.rows_by_item(tenant_id, SITE).get(item_id))


@pytest.mark.parametrize("order", ["deferred first", "due first"])
def test_a_due_sibling_captions_the_card_whichever_row_was_added_first(tenant, order):
    """Two `queued` rows for one item is ordinary, not exotic.

    `enqueue_autopilot_reply` adds without `unless_in_flight` and its
    `scheduled_at` is quiet-hours-clamped, so a draft queued at 23:00 lands +9h
    while a later one lands due-now — and the later one is the one going out.
    """
    _deal(tenant, "L1")
    offsets = [8, -1] if order == "deferred first" else [-1, 8]
    rows = [_add(tenant, "L1", auto=True, scheduled_at=_shift(o)) for o in offsets]
    due = rows[offsets.index(-1)]

    state = _governing(tenant, "L1")

    assert state["id"] == due["id"], (
        f"{order}: row {state['id']} spoke for the card, but row {due['id']} is "
        "the one the drainer takes on its next pass")
    assert state["label"] == "Queued to send…", (
        f"{order}: card reads {state['label']!r} over a message that is due "
        "now — the caption says there is still time to stop it when there is not")


@pytest.mark.parametrize("order", ["late first", "soon first"])
def test_the_soonest_deferred_row_names_the_deadline(tenant, order):
    """The operator reads that time to decide whether to intervene.

    Same defect one tier down: with every row deferred, the id tiebreak could
    still name the *later* stamp, so the card promised 08:00 tomorrow over a
    message leaving at 20:00 tonight. A caption carrying the wrong time is worse
    than one carrying none, because it is checkable and checks out wrong.
    """
    _deal(tenant, "L1")
    offsets = [8, 2] if order == "late first" else [2, 8]
    rows = [_add(tenant, "L1", auto=True, scheduled_at=_shift(o)) for o in offsets]
    soonest = rows[offsets.index(2)]

    state = _governing(tenant, "L1")

    assert state["label"] == "Scheduled to send", (
        f"{order}: nothing here is due, so the caption should not claim it is")
    assert state["scheduled_at"] == soonest["scheduled_at"], (
        f"{order}: card names {state['scheduled_at']} but the next message out "
        f"goes at {soonest['scheduled_at']} — the operator is reading a deadline "
        "that is hours late")


@pytest.mark.parametrize("order", ["sending first", "sending second"])
def test_a_send_already_going_out_outranks_a_deferred_sibling(tenant, order):
    """Non-regression for the precedence round 7 *did* get right, in both
    orders — the new rank must not trade one dimension away for the other."""
    _deal(tenant, "L1")
    first, second = _add(tenant, "L1", auto=True), _add(tenant, "L1", auto=True)
    live, deferred = (first, second) if order == "sending first" else (second, first)
    outbox.set_status(live["id"], outbox.SENDING)
    with outbox._conn() as c:
        c.execute("UPDATE outbox SET scheduled_at=? WHERE id=?",
                  (_shift(8), deferred["id"]))

    state = _governing(tenant, "L1")

    assert state["label"] == "Sending…", (
        f"{order}: card reads {state['label']!r} while a browser is already "
        "driving row %d" % live["id"])
    assert state["id"] == live["id"]


# --------------------------------------------------------------------------
# 7. The section has to list what is holding the guest, not just what you can
#    undo. Cancelling every blocker the page showed could still leave the
#    operator 409'd with nothing left to click.
# --------------------------------------------------------------------------

def test_a_sending_blocker_is_listed_even_though_it_cannot_be_called_off(
        client, tenant):
    _deal(tenant, "L1")
    blocker = _add(tenant, "L1", auto=True, body="the message going out now")
    outbox.set_status(blocker["id"], outbox.SENDING)

    html = SCRIPT_RE.sub("", client.get("/dashboard").get_data(as_text=True))

    assert outbox.get(blocker["id"])["status"] == outbox.SENDING, (
        "precondition: the reclaim requeued the row, so this asserts nothing")
    assert "the message going out now" in html, (
        "the one blocker the operator cannot clear is also the one the page "
        "does not mention: every visible blocker can be cancelled and the guest "
        "is still blocked, with nothing left to click")
    assert "cannot be called off" in html, (
        "listed without saying why it has no cancel, which reads as a missing "
        "button rather than a deliberate refusal")
    assert "going out now" in html, (
        "the section's row template states a plan — 'sends <time>' — and the "
        "stamp it prints for a row already being delivered is in the past")


def test_a_sending_blocker_is_not_offered_a_cancel_that_would_409(client, tenant):
    """Visibility is not the same as an affordance. `outbox.CANCELABLE` excludes
    `sending` for good reason — the route answers 409 — so rendering the button
    anyway would be the page lying about what it can do, one layer up."""
    _deal(tenant, "L1")
    blocker = _add(tenant, "L1", auto=True)
    outbox.set_status(blocker["id"], outbox.SENDING)

    html = SCRIPT_RE.sub("", client.get("/dashboard").get_data(as_text=True))

    assert not re.search(r"cancelMsg\(%d\)" % blocker["id"], html), (
        "offered 'Don't send' for a row already being delivered; the route "
        "refuses it with 409")
    assert client.post(f"/outbox/{blocker['id']}/cancel", data={}).status_code == 409, (
        "precondition: the route no longer refuses, so hiding the button is "
        "now the page under-reporting what the operator can do")


def test_a_queued_blocker_still_gets_its_working_cancel(client, tenant):
    """Control for the two above: widening the section to every in-flight row
    must not cost the clearable half its button."""
    _deal(tenant, "L1")
    blocker = _add(tenant, "L1", auto=True, body="still clearable")

    html = SCRIPT_RE.sub("", client.get("/dashboard").get_data(as_text=True))

    assert "still clearable" in html and re.search(
        r"cancelMsg\(%d\)" % blocker["id"], html), (
        "the queued blocker lost the cancel control it is supposed to have")


def test_the_render_that_reclaims_a_stranded_send_shows_the_reclaimed_state(
        client, tenant):
    """`_board` reads the outbox and self-heals it in the same request.

    Reading first meant the one render that freed a wedged row still described
    the state it had just replaced — `Sending…`, no cancel — while an
    /api/send-states poll seconds later already said `queued`. The next reload
    was correct, which is precisely what makes it hard to notice.
    """
    _deal(tenant, "L1")
    blocker = _add(tenant, "L1", auto=True, body="stranded by a crashed process")
    outbox.set_status(blocker["id"], outbox.SENDING)
    stale = (datetime.now(timezone.utc) - timedelta(seconds=5000)).isoformat()
    with outbox._conn() as c:
        c.execute("UPDATE outbox SET sending_at=?, attempts=0 WHERE id=?",
                  (stale, blocker["id"]))

    html = SCRIPT_RE.sub("", client.get("/dashboard").get_data(as_text=True))

    assert outbox.get(blocker["id"])["status"] == outbox.QUEUED, (
        "precondition: this render was supposed to reclaim the row")
    assert re.search(r"cancelMsg\(%d\)" % blocker["id"], html), (
        "the render that reclaimed the row still shows it as uncancelable, so "
        "the operator sees a blocker with no escape that the server had already "
        "made clearable")


@pytest.mark.parametrize("order", ["ascending", "descending"])
def test_rows_sharing_one_clamped_stamp_are_broken_the_drainer_s_way(tenant, order):
    """Identical `scheduled_at` is reachable, so the id tiebreak is load-bearing.

    `sequences._clamp_quiet_hours` returns `datetime.combine(date, QUIET_START)`
    — a *fixed* wake time — so two drafts queued at 23:10 and 23:47 both land on
    exactly 08:00. Nothing about the stamp separates them after that, and the
    card has to name the one the drainer will really take.

    So this asserts agreement with `next_queued` rather than a hardcoded id:
    the property is "the card names what goes out next", and pinning it to the
    drainer's own query is what keeps the two from drifting apart. Reversing the
    id term passes every other test in this file.
    """
    _deal(tenant, "L1")
    clamped = _shift(-1)                     # one stamp, both rows, already due
    rows = [_add(tenant, "L1", auto=True, scheduled_at=clamped, body="first"),
            _add(tenant, "L1", auto=True, scheduled_at=clamped, body="second")]
    if order == "descending":                # same tie, opposite list order
        rows = list(reversed(rows))
    assert rows[0]["scheduled_at"] == rows[1]["scheduled_at"], (
        "precondition: the two rows do not actually share a stamp, so this "
        "asserts nothing about the tiebreak")

    state = outbox.send_state([outbox.get(r["id"]) for r in rows])
    drainer_takes = outbox.next_queued(tenant)

    assert drainer_takes is not None, "precondition: neither row is deliverable"
    assert state["id"] == drainer_takes["id"], (
        f"{order}: the card is captioned by row {state['id']} but the drainer "
        f"takes row {drainer_takes['id']} next — with the stamps tied, the id "
        "order is the only thing left to agree on and it does not")
