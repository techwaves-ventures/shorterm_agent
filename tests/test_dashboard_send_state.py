"""VEN-131: the dashboard card must carry the item's real send state.

`templates/dashboard.html` gated its in-flight display on `send.in_flight`, but
the card carried the raw outbox row, whose keys are `outbox._COLS`. There is no
`in_flight` column, so the branch was dead: a `sending` message rendered no
state at all, `trackSends()` never resumed on load, and the card sat in "Needs
you now" with a live Approve & send over a message already going out.

The repo has no `GET /dashboard` test at all, which is why a dead Jinja branch
survived — a suite of 314 passing tests is entirely blind to this page. These
are the first, so they are deliberately explicit about the *seed*: two traps
here each produce a silent vacuous pass.

1. Without `ff_account.connect` + `mark_state("connected")`, the FF-verification
   gate swallows the whole board **and** the inline script, so every "is the
   string absent" assertion passes over a blank page.
2. `_board` calls `reclaim_stuck_sending()` on the read path, so a `sending` row
   whose `approved_at` is empty is silently requeued mid-test. Seeding through
   `add() -> QUEUED -> SENDING` writes the stamps that keeps it in flight.

Hence the positive control in `_assert_card_rendered`: assert the card *is*
there before asserting anything about it, or an empty page proves whatever you
like.

Not every test here fails without the fix, and the earlier claim that they all
did was measured false: run against a bare `6f62a57`, 15 fail and 7 pass. Five
of the seven are deliberate settled-state controls and are supposed to pass on
both trees. The other two were vacuous and are fixed in place — see
`test_sent_state_agrees_too` and `test_the_board_issues_no_more_queries_than_before`.
State the number, not the adjective: "every test fails on base" is exactly the
kind of claim that persuades the next reviewer to skip checking.
"""
import os
import re
import tempfile

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("FF_CRED_KEY", "c9jwUi0L-fUjf3wjbq74M0lK3ah7fmEfGhjxZ7RehQk=")
os.environ.setdefault("SECRET_KEY", "test-secret")

import config  # noqa: E402
import ff_account  # noqa: E402
import outbox  # noqa: E402
import pipeline  # noqa: E402
import storage  # noqa: E402

SITE = "furnishedfinder"
EMAIL = "host@example.com"
PASSWORD = "a-perfectly-fine-passphrase"

SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.S | re.I)


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    """A logged-in-able tenant whose dashboard actually renders a board."""
    import db
    import models

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ven131.db")
    monkeypatch.setattr(pipeline, "_TS_NORMALIZED", False, raising=False)
    monkeypatch.setenv("INSECURE_COOKIES", "1")

    user = models.create_user(EMAIL, PASSWORD)
    tid = str(user.tenant_id)
    config.save_settings(tid, host_name="Test Host", timezone="America/New_York",
                         onboarded="1")
    # Trap 1: without a connected FF account the template renders the
    # verification note *instead of* the board.
    ff_account.connect(tid, "ff@example.com")
    ff_account.mark_state(tid, "connected")
    return tid


@pytest.fixture()
def client(tenant, monkeypatch):
    import automation
    import dashboard

    # The read path starts a drainer, which would deliver the fixture out from
    # under the assertions.
    monkeypatch.setattr(automation, "start_drainer", lambda *a, **k: None)
    dashboard.app.config["TESTING"] = True
    dashboard.app.config["WTF_CSRF_ENABLED"] = False
    c = dashboard.app.test_client()
    resp = c.post("/login", data={"email": EMAIL, "password": PASSWORD})
    # Positive control: a CSRF 400 or a failed login impersonates every defect
    # below by rendering a page with no card on it.
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


def _row(tenant_id, item_id, status, *, body="hello"):
    """One outbox row driven into `status` the way the app drives it.

    `auto=True` lands straight in `queued` with `approved_at` stamped, which is
    what keeps trap 2 shut: a `sending` row with an empty `approved_at` is
    silently requeued by the reclaim on the dashboard read path.
    """
    msg = outbox.add(tenant_id, SITE, item_id, sequence="presale",
                     step_id="intro", step_label="First reply",
                     body=body, auto=status != outbox.PENDING)
    msg_id = msg["id"]
    assert outbox.get(msg_id)["status"] == (
        outbox.PENDING if status == outbox.PENDING else outbox.QUEUED
    ), "precondition: add() did not land in the status this helper assumes"
    if status in (outbox.PENDING, outbox.QUEUED):
        return msg_id
    outbox.set_status(msg_id, status)
    assert outbox.get(msg_id)["status"] == status
    return msg_id


def _rows_for(tenant_id, item_id):
    """Every row for one item, read without going through the new helper.

    The regression tests must fail on the base commit for the *filed* reason —
    an `AttributeError` on an API that base does not have would prove nothing.
    """
    with outbox._conn() as c:
        return c.execute(
            "SELECT id, status FROM outbox WHERE tenant_id=? AND site=? "
            "AND item_id=? ORDER BY id ASC",
            (str(tenant_id), SITE, str(item_id)),
        ).fetchall()


def _dashboard_html(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200, f"/dashboard returned {resp.status_code}"
    return resp.get_data(as_text=True)


def _server_html(html):
    """The markup with every <script> stripped — i.e. what renders before JS."""
    return SCRIPT_RE.sub("", html)


def _assert_card_rendered(html, item_id):
    """The positive control every negative assertion below depends on."""
    assert f'id="msg-{item_id}"' in html or f'id="send-{item_id}"' in html, (
        "precondition failed: the card is not on the page at all, so any "
        "assertion about its contents would pass vacuously"
    )


def _button(html, elem_id):
    m = re.search(r"<button[^>]*\bid=\"%s\"[^>]*>" % re.escape(elem_id), html)
    assert m, f"no button with id={elem_id!r} in the rendered page"
    return m.group(0)


# --------------------------------------------------------------------------
# 1-3: the filed defect
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status,label", [
    (outbox.SENDING, "Sending…"),
    (outbox.QUEUED, "Queued to send…"),
])
def test_an_in_flight_send_is_visible_in_the_server_rendered_page(
        client, tenant, status, label):
    """AC1: the state is in the markup, not conjured later by a fetch."""
    _deal(tenant, "s1")
    _row(tenant, "s1", status)

    html = _dashboard_html(client)
    _assert_card_rendered(html, "s1")
    assert label in _server_html(html), (
        f"a {status} message renders no in-flight state on /dashboard "
        f"before any JS runs"
    )


@pytest.mark.parametrize("status", [outbox.SENDING, outbox.QUEUED])
def test_the_approve_button_is_disabled_in_the_server_html(client, tenant, status):
    """AC2: disabled by the server. JS disabling it needs a round-trip, and
    every load before that lands offers a send over a live one."""
    _deal(tenant, "s1")
    _row(tenant, "s1", status)

    html = _server_html(_dashboard_html(client))
    _assert_card_rendered(html, "s1")
    assert "disabled" in _button(html, "send-s1"), (
        "Approve & send is live in the server HTML over an in-flight message"
    )


def test_the_poll_is_armed_on_load_with_no_user_action(client, tenant):
    """AC3/AC9: `trackSends()` runs on load, so state is picked up without a
    click and a send that starts later in the session is still seen."""
    _deal(tenant, "s1")
    _row(tenant, "s1", outbox.SENDING)

    html = _dashboard_html(client)
    _assert_card_rendered(html, "s1")
    scripts = "\n".join(SCRIPT_RE.findall(html))

    # Anchored to top-level indentation on purpose: `sendReply` also calls
    # `trackSends()`, and a loose `\s*` match finds *that* one and passes on a
    # page whose poll is never armed on load at all.
    assert re.search(r"^      trackSends\(\);$", scripts, re.M), (
        "nothing arms the delivery poll on load (the call inside sendReply "
        "only runs after a click)"
    )
    # The old else-branch fetched once and started no interval, so a send that
    # began later in the session was never picked up.
    assert 'if (s.status === "failed" || s.status === "sent") renderSendState' \
        not in scripts, "the one-shot fallback still short-circuits the poll"


@pytest.mark.parametrize("status", [outbox.SENDING, outbox.QUEUED])
def test_an_in_flight_button_carries_its_resting_caption(client, tenant, status):
    """Found in the browser, not by the suite.

    `setBusy` caches `innerHTML` the first time it makes a button busy and
    restores it on the way out. Server-rendering the caption as "Sending…"
    poisoned that cache: when the send settled, `setBusy(btn, false)` handed the
    button back **enabled and still captioned "Sending…"** — a live control
    claiming to be mid-send, which is the ticket's own complaint inverted.
    """
    _deal(tenant, "s1")
    _row(tenant, "s1", status)

    html = _server_html(_dashboard_html(client))
    btn = _button(html, "send-s1")
    assert 'data-label="Approve &amp; send"' in btn, (
        "no resting caption, so setBusy will restore this button to whatever "
        "the server rendered while it was in flight"
    )


def test_the_client_tests_in_flight_before_status(client, tenant):
    """The shared helper does not close the defect on its own.

    `renderSendState` branched on `status` first: `sent`, then `failed`, then
    `in_flight`. A faithful feed of `status:"failed", in_flight:true` — an
    effective state this fix can now produce — therefore lands in the `failed`
    branch, which runs `btn.disabled = false; btn.textContent = "Retry send"`
    and re-enables the button over a queued send.

    Asserted structurally because this branch is JS and the suite is Python;
    the behaviour itself is exercised in the browser (see the PR evidence).
    Without this, reverting the reordering leaves the whole suite green.
    """
    _deal(tenant, "s1")
    _row(tenant, "s1", outbox.SENDING)
    scripts = "\n".join(SCRIPT_RE.findall(_dashboard_html(client)))

    body = re.search(r"function renderSendState\(id, s\) \{(.*?)\n      \}",
                     scripts, re.S)
    assert body, "renderSendState is not in the rendered page"
    body = body.group(1)

    at_in_flight = body.find("s.in_flight")
    at_failed = body.find('s.status === "failed"')
    at_sent = body.find('s.status === "sent"')
    assert at_in_flight != -1 and at_failed != -1 and at_sent != -1
    assert at_in_flight < at_failed and at_in_flight < at_sent, (
        "renderSendState decides on `status` before `in_flight`, so an "
        "effective status of failed/sent re-enables the button over a send "
        "that is still in flight"
    )
    # The in-flight branch must not fall through into the ones that re-enable.
    assert "return;" in body[at_in_flight:at_failed], (
        "the in-flight branch falls through to a branch that re-enables"
    )


def test_the_two_surfaces_do_not_disagree_about_the_wording(client, tenant):
    """The server card and the poll are meant to be one rule, and printed two
    different strings for one state: the card said "Queued to send…" and the
    first poll rewrote the same button to "Queued…". The label is the server's,
    so there is one place it can be changed.
    """
    _deal(tenant, "s1")
    _row(tenant, "s1", outbox.QUEUED)

    html = _dashboard_html(client)
    # Comments stripped: the first version of this test matched the comment that
    # *explains* the old wording and failed on a correct page.
    code = re.sub(r"//[^\n]*", "", "\n".join(SCRIPT_RE.findall(html)))

    assert outbox.STATUS_LABELS[outbox.QUEUED] in _server_html(html)
    assert '"Queued…"' not in code, (
        "the poll carries its own copy of the wording, which drifted from the "
        "server's the moment either changed"
    )
    assert "s.label" in code, "the poll no longer renders the server's label"
    assert client.get("/api/send-states").get_json()["s1"]["label"] == (
        outbox.STATUS_LABELS[outbox.QUEUED])


def test_starting_a_send_does_not_wait_out_the_idle_backoff(client, tenant):
    """`trackSends()` returns early once the poll is armed — and it is armed for
    the life of the page and never cleared. It left `idleTicks` at 4, so a send
    started during an idle stretch showed nothing for up to 15s: the tick that
    would have rendered it was skipped four times first.

    Structural, like the branch-order tripwire: the reset must happen *before*
    the early return, or arming order decides whether it runs.
    """
    _deal(tenant, "s1")
    _row(tenant, "s1", outbox.QUEUED)
    scripts = "\n".join(SCRIPT_RE.findall(_dashboard_html(client)))

    body = re.search(r"async function trackSends\(\) \{(.*?)\n      \}",
                     scripts, re.S)
    assert body, "trackSends is not in the rendered page"
    body = body.group(1)

    at_reset = body.find("idleTicks = 0")
    at_return = body.find("if (sendPoll) return;")
    assert at_reset != -1, "trackSends never resets the idle-skip counter"
    assert at_return != -1, "the early return this test guards is gone"
    assert at_reset < at_return, (
        "idleTicks is reset after the early return, so every call but the first "
        "leaves the backoff in place and the send goes unrendered for ~15s"
    )


# --------------------------------------------------------------------------
# 4: the divergence — what separates the correct fix from the cheap one
# --------------------------------------------------------------------------

@pytest.mark.parametrize("newer", [outbox.PENDING, outbox.CANCELED, outbox.FAILED])
def test_an_older_in_flight_row_beats_a_newer_settled_one(client, tenant, newer):
    """AC4. Two rows for one item is ordinary: `enqueue_autopilot_reply` has no
    per-item dedupe, so one scrape returning two messages in a thread queues
    two. Last-write-wins then reports the item idle while the older row is
    genuinely going out — the exact state this ticket exists to prevent.
    """
    _deal(tenant, "s1")
    in_flight = _row(tenant, "s1", outbox.SENDING, body="first")
    _row(tenant, "s1", newer, body="second")

    assert outbox.get(in_flight)["status"] == outbox.SENDING, (
        "precondition: the older row is still in flight"
    )

    html = _server_html(_dashboard_html(client))
    _assert_card_rendered(html, "s1")
    assert "Sending…" in html, (
        f"an older `sending` row is invisible behind a newer `{newer}` row"
    )
    assert "disabled" in _button(html, "send-s1"), (
        f"Approve & send is live over an in-flight send because a newer "
        f"`{newer}` row won the card"
    )


def test_the_api_agrees_with_the_card_on_the_divergence(client, tenant):
    """AC6: the page polls this endpoint, so if it keeps the cheap rule the
    client is told to re-enable the button and cannot recover."""
    _deal(tenant, "s1")
    _row(tenant, "s1", outbox.SENDING, body="first")
    _row(tenant, "s1", outbox.CANCELED, body="second")

    state = client.get("/api/send-states").get_json()["s1"]
    assert state["in_flight"] is True, (
        "/api/send-states reports an item idle while one of its own rows is "
        "in flight"
    )
    assert state["status"] == outbox.SENDING


def test_sent_state_agrees_too(client, tenant):
    """AC6 for the third caller — /responder/send and the thread page both read
    `_sent_state`.

    Billed as proof of this change, this was vacuous: base `_sent_state` already
    filtered `for_tenant(..., IN_FLIGHT)` by item, so it already had any-in-flight
    semantics and passed on `6f62a57` unchanged. Kept as a regression guard on
    the agreement, and given the case that does discriminate — an item holding
    both a `queued` and a `sending` row must be described by the `sending` one.
    Taking the first in-flight row by id said "Queued to send…" over a message a
    browser was already delivering, which reads as "there is still time".
    """
    import dashboard

    _deal(tenant, "s1")
    _row(tenant, "s1", outbox.SENDING, body="first")
    _row(tenant, "s1", outbox.CANCELED, body="second")

    blocked = dashboard._sent_state(tenant, "s1", {"status": "draft",
                                                   "draft": "something new"})
    assert blocked == "Sending…", (
        f"_sent_state offered the send button over an in-flight row: {blocked!r}"
    )

    # The discriminating case: older row `queued`, newer row `sending`.
    _deal(tenant, "s2")
    _row(tenant, "s2", outbox.QUEUED, body="first")
    _row(tenant, "s2", outbox.SENDING, body="second")

    assert dashboard._sent_state(tenant, "s2", None) == "Sending…", (
        "an item with a live browser send was described by its merely-queued row"
    )
    grouped = outbox.rows_by_item(tenant, SITE)["s2"]
    assert [m["status"] for m in grouped] == [outbox.QUEUED, outbox.SENDING], (
        "precondition: the seed did not produce queued-then-sending in id order, "
        "so 'first in-flight row by id' and 'the sending one' would not differ"
    )
    assert outbox.send_state(grouped)["status"] == outbox.SENDING
    assert client.get("/api/send-states").get_json()["s2"]["label"] == "Sending…"


# --------------------------------------------------------------------------
# 5: D1 — the server-side double-send this route still accepted
# --------------------------------------------------------------------------

def test_approving_a_sibling_row_mid_flight_is_refused(client, tenant):
    """AC5. `outbox_approve` checked only the row being approved, so the second
    Approve & send button released a *second* message into a thread already
    mid-delivery and answered 200. VEN-127's 409 covers /responder/send, not
    this route."""
    _deal(tenant, "s1")
    _row(tenant, "s1", outbox.SENDING, body="first")
    pending = _row(tenant, "s1", outbox.PENDING, body="second")

    before = len(_rows_for(tenant, "s1"))
    resp = client.post(f"/outbox/{pending}/approve", data={"text": "second"})

    assert resp.status_code == 409, (
        f"approve released a second message while one was in flight: "
        f"{resp.status_code} {resp.get_data(as_text=True)[:200]}"
    )
    assert outbox.get(pending)["status"] == outbox.PENDING, (
        "the sibling row was released despite the refusal"
    )
    assert len(_rows_for(tenant, "s1")) == before, (
        "the refused approve still added a row"
    )


def test_the_second_approve_button_is_inert_while_a_sibling_is_in_flight(
        client, tenant):
    """The UI half of the same defect: that button had no `id` at all, so the
    poll could never reach it however correct the state feed became."""
    _deal(tenant, "s1")
    _row(tenant, "s1", outbox.SENDING, body="first")
    pending = _row(tenant, "s1", outbox.PENDING, body="second")

    html = _server_html(_dashboard_html(client))
    btn = _button(html, f"approve-ob-{pending}")
    assert "disabled" in btn, (
        "the awaiting-approval Approve & send is live over an in-flight sibling"
    )


def test_approve_still_works_when_nothing_is_in_flight(client, tenant):
    """The guard must not swallow the ordinary case — a 409 for everyone is
    also a way to make this test file green."""
    _deal(tenant, "s1")
    pending = _row(tenant, "s1", outbox.PENDING, body="only")

    resp = client.post(f"/outbox/{pending}/approve", data={"text": "only"})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:200]
    assert outbox.get(pending)["status"] == outbox.QUEUED


# --------------------------------------------------------------------------
# 6: every remaining way to put a second message in front of one guest
#
# The guard was added per route, and per-route missed a route three times on
# this ticket: the send button, then the second approve button, then
# `/outbox/<id>/retry`. So these test the *choke point* — for each surface that
# can move a row into `IN_FLIGHT`, and for the interleaving as well as the
# sequence, because a check-then-act guard passes every sequential test.
# --------------------------------------------------------------------------

def _in_flight_count(tenant_id, item_id):
    return sum(status in outbox.IN_FLIGHT
               for _id, status in _rows_for(tenant_id, item_id))


def test_retrying_beside_an_in_flight_sibling_is_refused(client, tenant):
    """`/outbox/<id>/retry` re-queued a failed row while a sibling of the same
    item was still `sending`, and answered 200 — two messages in flight to one
    guest. It carried no sibling guard at all; the approve route's guard did not
    reach it, which is the argument for guarding the write instead of the route.
    """
    _deal(tenant, "s1")
    _row(tenant, "s1", outbox.SENDING, body="first")
    failed = _row(tenant, "s1", outbox.FAILED, body="second")

    resp = client.post(f"/outbox/{failed}/retry")

    assert resp.status_code == 409, (
        f"retry released a second message while one was in flight: "
        f"{resp.status_code} {resp.get_data(as_text=True)[:200]}"
    )
    assert resp.get_json()["error"] == "Sending…", resp.get_json()
    assert outbox.get(failed)["status"] == outbox.FAILED
    assert _in_flight_count(tenant, "s1") == 1


def test_retry_still_works_when_nothing_is_in_flight(client, tenant):
    """The control for the test above — refusing every retry is also green."""
    _deal(tenant, "s1")
    failed = _row(tenant, "s1", outbox.FAILED, body="only")

    resp = client.post(f"/outbox/{failed}/retry")
    assert resp.status_code == 200, resp.get_data(as_text=True)[:200]
    assert outbox.get(failed)["status"] == outbox.QUEUED


def test_two_approvals_racing_cannot_both_win(client, tenant):
    """The guard was a read taken before the write, so two requests that both
    read "nothing in flight" both released. Driven at the layer the route calls,
    with both reads forced to complete before either write.

    Measured on the read-then-write shape this replaces: two concurrent approves
    both returned 200 and left two rows `queued`.
    """
    _deal(tenant, "s1")
    a = _row(tenant, "s1", outbox.PENDING, body="first")
    b = _row(tenant, "s1", outbox.PENDING, body="second")

    import threading
    barrier = threading.Barrier(2)
    won = {}

    def approve(msg_id):
        barrier.wait()
        for _ in range(200):      # SQLite serializes writers; retry the lock
            try:
                won[msg_id] = outbox.release_to_send(
                    msg_id, from_statuses=outbox.APPROVABLE)[0]
                return
            except Exception as exc:            # pragma: no cover - lock only
                if "locked" not in str(exc).lower():
                    raise
        won[msg_id] = "never got the lock"

    threads = [threading.Thread(target=approve, args=(m,)) for m in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(won.values(), key=str) == [False, True], (
        f"both concurrent approvals were told they had released a message: {won}"
    )
    assert _in_flight_count(tenant, "s1") == 1, (
        f"two messages in flight for one guest: {_rows_for(tenant, 's1')}"
    )


def test_the_send_button_cannot_queue_two_by_racing_itself(client, tenant):
    """`/responder/send` reads `_sent_state` and then *inserts*. Two clicks that
    both read before either inserted both queued a message: measured 137 of 150
    concurrent pairs put two messages in front of one guest, and the button
    being disabled client-side is not a guard against a stale tab or a replay.

    The insert now carries the same condition, so this is the insert-shaped half
    of the same rule `release_to_send` applies to updates.
    """
    import automation

    _deal(tenant, "s1")
    _row(tenant, "s1", outbox.SENDING, body="already going out")

    assert automation.enqueue_send(tenant, SITE, "s1", "second") is None, (
        "a second message was queued while one was already in flight"
    )
    assert _in_flight_count(tenant, "s1") == 1

    resp = client.post("/responder/send", data={"item_id": "s1", "text": "second"})
    assert resp.status_code == 409, resp.get_data(as_text=True)[:200]
    assert _in_flight_count(tenant, "s1") == 1


def test_the_send_button_still_queues_when_the_thread_is_idle(client, tenant):
    """Control: the guard must not refuse the ordinary first send."""
    import automation

    _deal(tenant, "s1")
    msg = automation.enqueue_send(tenant, SITE, "s1", "hello")
    assert msg is not None and msg["status"] == outbox.QUEUED
    assert _in_flight_count(tenant, "s1") == 1


def test_the_guard_lives_on_the_write_not_on_the_route(client, tenant):
    """The reason the three tests above can be trusted not to rot.

    Each of those drives one route. This asserts the property that makes a
    *fourth* route safe by construction: `set_status` refuses to move a row into
    `queued` beside an in-flight sibling, so a new caller cannot reach the write
    without the check. A route-level guard would leave this passing while the
    next route is added unguarded.
    """
    _deal(tenant, "s1")
    _row(tenant, "s1", outbox.SENDING, body="first")
    pending = _row(tenant, "s1", outbox.PENDING, body="second")

    assert outbox.set_status(pending, outbox.QUEUED,
                             unless_sibling_in_flight=True) is False
    assert outbox.get(pending)["status"] == outbox.PENDING

    # ...and `only_from` is the other half: a settled row cannot be re-released
    # even with nothing in flight beside it.
    _deal(tenant, "s2")
    sent = _row(tenant, "s2", outbox.SENT, body="already read")
    assert outbox.set_status(sent, outbox.QUEUED,
                             only_from=outbox.APPROVABLE) is False
    assert outbox.get(sent)["status"] == outbox.SENT

    # Both guards off is still the old unconditional write, so callers that pass
    # neither are unaffected.
    assert outbox.set_status(sent, outbox.CANCELED) is True


def test_a_row_is_not_its_own_blocker(client, tenant):
    """`NOT EXISTS (... id<>outbox.id ...)`: a row already `queued` must still be
    writable, or the drainer's own `queued`->`sending` claim would deadlock and
    nothing would ever be delivered."""
    _deal(tenant, "s1")
    queued = _row(tenant, "s1", outbox.QUEUED, body="only")

    assert outbox.set_status(queued, outbox.SENDING,
                             unless_sibling_in_flight=True) is True
    assert outbox.get(queued)["status"] == outbox.SENDING


# --------------------------------------------------------------------------
# 7: the states that must not change
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", [outbox.SENT, outbox.FAILED, outbox.CANCELED])
def test_settled_states_leave_the_button_live_or_not_as_before(
        client, tenant, status):
    """AC7: only in-flight items change. A fix that disables the button
    whenever any row exists would pass every test above."""
    _deal(tenant, "s1")
    _row(tenant, "s1", status)

    html = _server_html(_dashboard_html(client))
    _assert_card_rendered(html, "s1")
    assert "disabled" not in _button(html, "send-s1"), (
        f"a `{status}` row disabled the server-rendered button, which is a "
        f"behaviour change beyond this ticket"
    )
    assert "Sending…" not in html and "Queued to send…" not in html


def test_a_card_with_no_outbox_row_is_untouched(client, tenant):
    _deal(tenant, "s1")

    html = _server_html(_dashboard_html(client))
    _assert_card_rendered(html, "s1")
    assert "disabled" not in _button(html, "send-s1")


# --------------------------------------------------------------------------
# 8: the shared rule itself
# --------------------------------------------------------------------------

def test_send_state_is_pure_and_falls_back_to_the_newest_row():
    """Single-row items — the common case — must read exactly as
    reading the newest row alone made them read (`latest_by_item`, since
    removed as the last caller went away)."""
    assert outbox.send_state(None) is None
    assert outbox.send_state([]) is None

    rows = [{"id": 1, "status": outbox.SENT, "error": None, "step_label": "a"},
            {"id": 2, "status": outbox.FAILED, "error": "boom", "step_label": "b"}]
    state = outbox.send_state(rows)
    assert state["status"] == outbox.FAILED and state["in_flight"] is False
    assert state["error"] == "boom" and state["step"] == "b"

    rows.insert(0, {"id": 0, "status": outbox.QUEUED, "error": None,
                    "step_label": "z"})
    state = outbox.send_state(rows)
    assert state["status"] == outbox.QUEUED and state["in_flight"] is True
    assert state["label"] == "Queued to send…"


def test_the_board_issues_no_more_queries_than_before(client, tenant, monkeypatch):
    """AC8: `rows_by_item` reads every row for the tenant once and groups them in
    Python, so a card may ask it anything without paying per card.

    This asserted `<= 12` against a measured 6 — 2x slack, which made it vacuous
    for the regression its own docstring named: adding a second whole-table read
    alongside the grouped one still passed. Two assertions now, because there are
    two distinct regressions and a single number catches only one:

    * **constant** in the number of items — a read moved inside `card()` is an
      N+1, and it is the one that gets worse in production, not in this test;
    * a **tight** absolute bound, so a second full read added alongside the
      grouped one has to be a deliberate change to this number rather than
      slack someone else already paid for.
    """
    import dashboard

    def board_conn_count(n_items, first_item):
        for i in range(n_items):
            item = f"{first_item}{i}"
            _deal(tenant, item, guest=f"Guest {i}")
            _row(tenant, item, outbox.SENDING)
        calls = {"n": 0}
        real_conn = outbox._conn

        def counting_conn(*a, **k):
            calls["n"] += 1
            return real_conn(*a, **k)

        monkeypatch.setattr(outbox, "_conn", counting_conn)
        dashboard._board(tenant)
        monkeypatch.setattr(outbox, "_conn", real_conn)
        return calls["n"]

    few = board_conn_count(5, "q")
    many = board_conn_count(15, "r")   # 20 items on the board by now

    assert few == many, (
        f"_board opened {few} outbox connections for 5 items and {many} for 20 — "
        f"it scales with the board, which is the per-card N+1 this design avoids"
    )
    assert few <= 6, (
        f"_board opened {few} outbox connections; 6 is the measured cost of the "
        f"grouped read. A higher number means a whole-table read was added "
        f"alongside it rather than replacing it — raise this deliberately."
    )
