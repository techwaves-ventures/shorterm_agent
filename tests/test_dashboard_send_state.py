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

Every test here fails on `6f62a57` for the filed reason.
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
    """AC6 for the third caller — the guard /responder/send and the thread page
    both read `_sent_state`."""
    import dashboard

    _deal(tenant, "s1")
    _row(tenant, "s1", outbox.SENDING, body="first")
    _row(tenant, "s1", outbox.CANCELED, body="second")

    blocked = dashboard._sent_state(tenant, "s1", {"status": "draft",
                                                   "draft": "something new"})
    assert blocked == "Sending…", (
        f"_sent_state offered the send button over an in-flight row: {blocked!r}"
    )


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
    `latest_by_item` made them read."""
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
    """AC8: `latest_by_item` already selected every row for the tenant and
    collapsed in Python, so grouping is free — but only if the grouped call
    *replaces* it. Adding it alongside costs a connection per board render,
    because `_conn()` runs CREATE TABLE IF NOT EXISTS + PRAGMA on every open.
    """
    import dashboard

    for i in range(5):
        _deal(tenant, f"q{i}", guest=f"Guest {i}")
        _row(tenant, f"q{i}", outbox.SENDING)

    calls = {"n": 0}
    real_conn = outbox._conn

    def counting_conn(*a, **k):
        calls["n"] += 1
        return real_conn(*a, **k)

    monkeypatch.setattr(outbox, "_conn", counting_conn)
    dashboard._board(tenant)
    grouped = calls["n"]

    assert grouped <= 12, (
        f"_board opened {grouped} outbox connections; the per-card N+1 this "
        f"design exists to avoid would scale with the 5 seeded items"
    )
