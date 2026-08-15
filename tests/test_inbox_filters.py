"""Tests for the inbox: filtering, counting, pagination, and state agreement.

The operator's ask was "I need to be able to see all the leads, all the
messages, open / closed / responded" — so the thing under test is whether one
query can answer that, and whether it still answers it the same way the rest of
the app does.

That last part is the risk worth a test file of its own. `pipeline.lead_state()`
is the Python definition of a lead's state; `pipeline._state_sql()` is the SQL
one, and it exists because filtering and paginating in Python would mean loading
every deal for the tenant to render 25 of them. Two implementations of one rule
is exactly the drift this work set out to remove, so the matrix test below runs
every interesting deal shape through both and asserts they agree.
"""
import os
import tempfile

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("FF_CRED_KEY", "c9jwUi0L-fUjf3wjbq74M0lK3ah7fmEfGhjxZ7RehQk=")
os.environ.setdefault("SECRET_KEY", "test-secret")

import config  # noqa: E402
import pipeline  # noqa: E402
import storage  # noqa: E402

SITE = "furnishedfinder"


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "inbox.db")
    tid = "1"
    config.save_settings(tid, host_name="Test Host", timezone="America/New_York")
    return tid


def _deal(tenant_id, item_id, *, kind="lead", guest="Dana R.", unit=None,
          stage=pipeline.NEW, inquiry="2026-08-01T09:00:00", response=None,
          **fields):
    """One deal plus its stored item and (optional) responder decision."""
    item = {"id": item_id, "kind": kind, "traveler": guest,
            "title": f"Unit | {guest}", "property_name": unit or ""}
    storage.filter_new(tenant_id, SITE, kind, [item])
    pipeline.ensure(tenant_id, SITE, item, {"unit_id": unit} if unit else None)
    pipeline.update(tenant_id, SITE, item_id, stage=stage, **fields)
    with pipeline._conn() as c:
        c.execute("UPDATE deals SET inquiry_at=? WHERE tenant_id=? AND item_id=?",
                  (inquiry, tenant_id, item_id))
    if response:
        storage.save_response(tenant_id, SITE, kind, item_id, **response)
    return item


# --- the two definitions of "state" must agree -------------------------------

# Every branch of lead_state(), as (deal fields, response, is-a-failed-send).
STATE_MATRIX = [
    ({"stage": pipeline.NEW}, None, False),
    ({"stage": pipeline.NEW}, {"status": "draft", "draft": "hi"}, False),
    ({"stage": pipeline.CONTACTED}, {"status": "sent"}, False),
    ({"stage": pipeline.CONTACTED}, {"status": "dismissed"}, False),
    ({"stage": pipeline.NEW}, {"status": "skipped", "reason": "Wrong city."}, False),
    ({"stage": pipeline.NEW},
     {"status": "skipped", "reason": "draft error: API key missing"}, False),
    ({"stage": pipeline.NURTURING, "next_action_at": "2099-01-01T09:00:00"},
     {"status": "sent"}, False),
    ({"stage": pipeline.BOOKED}, {"status": "sent"}, False),
    ({"stage": pipeline.PRE_ARRIVAL}, {"status": "sent"}, False),
    ({"stage": pipeline.STAYING}, {"status": "sent"}, False),
    ({"stage": pipeline.COMPLETED}, {"status": "sent"}, False),
    ({"stage": pipeline.LOST}, None, False),
    ({"stage": pipeline.CONTACTED, "last_contact_at": "2026-08-01T10:00:00",
      "last_guest_reply_at": "2026-08-01T11:00:00"}, {"status": "sent"}, False),
    ({"stage": pipeline.CONTACTED, "last_contact_at": "2026-08-01T12:00:00",
      "last_guest_reply_at": "2026-08-01T11:00:00"}, {"status": "sent"}, False),
    # A guest reply with no outbound at all (they wrote first, then again).
    ({"stage": pipeline.NEW, "last_guest_reply_at": "2026-08-01T11:00:00"},
     None, False),
    ({"stage": pipeline.CONTACTED}, {"status": "sent"}, True),
    ({"stage": pipeline.NURTURING, "next_action_at": "2099-01-01T09:00:00"},
     {"status": "sent"}, True),
]


@pytest.mark.parametrize("fields,response,failed", STATE_MATRIX)
def test_sql_and_python_state_agree(tenant, fields, response, failed):
    """If these two ever disagree, the tab bar counts one thing and every other
    surface in the app believes another."""
    _deal(tenant, "D1", response=response, **fields)
    failed_ids = ("D1",) if failed else ()

    page = pipeline.inbox_page(tenant, SITE, failed_item_ids=failed_ids)
    assert len(page["rows"]) == 1
    row = page["rows"][0]
    expected = pipeline.lead_state(row["deal"], row["response"],
                                   has_failed_send=failed)
    assert row["state"] == expected


def test_sql_and_python_agree_with_every_filter_combined(tenant):
    """The matrix above checks one deal at a time with no filters. This checks
    the parameter binding, which is where this query can silently go wrong.

    The state CASE appears in the SELECT list *and* again in the WHERE clause
    when filtering by state, so its parameters bind twice, around the parameters
    for kind/unit/search. Get that order wrong and the query still runs — it
    just answers a different question than the one asked.
    """
    import itertools

    stages = [pipeline.NEW, pipeline.CONTACTED, pipeline.NURTURING,
              pipeline.BOOKED, pipeline.LOST]
    statuses = [None, "draft", "sent", "skipped", "dismissed"]
    n = 0
    for stage, status, kind, unit in itertools.product(
            stages, statuses, ("lead", "message"), ("unit-1", "unit-2")):
        n += 1
        item_id = f"i{n}"
        fields = {"stage": stage, "unit_id": unit}
        if n % 3 == 0:  # some guests have written back since our last message
            fields["last_guest_reply_at"] = "2026-08-01T11:00:00"
            fields["last_contact_at"] = "2026-08-01T10:00:00"
        if n % 5 == 0:
            fields["next_action_at"] = "2099-01-01T09:00:00"
        response = {"status": status,
                    "reason": "draft error: boom" if n % 7 == 0 else "ok"}
        _deal(tenant, item_id, kind=kind, guest=f"Guest {n % 7}",
              response=response if status else None, **fields)
        # unit has to be set after ensure(), which derives it from the response
        pipeline.update(tenant, SITE, item_id, unit_id=unit)

    failed = tuple(f"i{i}" for i in range(1, n + 1) if i % 11 == 0)
    checked = 0
    for state, kind, unit, q in itertools.product(
            [None, *pipeline.LEAD_STATES], [None, "lead", "message"],
            [None, "unit-1"], [None, "guest 3"]):
        page = pipeline.inbox_page(tenant, SITE, state=state, kind=kind,
                                   unit=unit, q=q, per_page=100,
                                   failed_item_ids=failed)
        for row in page["rows"]:
            checked += 1
            deal = row["deal"]
            assert row["state"] == pipeline.lead_state(
                deal, row["response"],
                has_failed_send=deal["item_id"] in failed)
            # Every filter must actually hold on every returned row.
            assert not state or row["state"] == state
            # "Messages" is every conversation the guest has written in, not
            # just the deals whose *origin* was a direct message — a reply that
            # threads onto a lead never gets a deal row of its own.
            if kind == "message":
                assert (deal["kind"] == "message"
                        or deal["last_guest_reply_at"]), deal
            elif kind:
                assert deal["kind"] == kind
            assert not unit or deal["unit_id"] == unit
    assert checked > 500, "the sweep has to actually return rows to prove anything"


# --- filtering ---------------------------------------------------------------


def test_filter_by_state_answers_open_closed_responded(tenant):
    _deal(tenant, "needs", stage=pipeline.NEW)
    _deal(tenant, "waiting", stage=pipeline.CONTACTED, response={"status": "sent"})
    _deal(tenant, "won", stage=pipeline.BOOKED, response={"status": "sent"})
    _deal(tenant, "replied", stage=pipeline.CONTACTED,
          last_contact_at="2026-08-01T10:00:00",
          last_guest_reply_at="2026-08-01T11:00:00", response={"status": "sent"})

    def ids(**kw):
        return {r["deal"]["item_id"]
                for r in pipeline.inbox_page(tenant, SITE, **kw)["rows"]}

    assert ids(state=pipeline.NEEDS_YOU) == {"needs"}
    assert ids(state=pipeline.AWAITING_GUEST) == {"waiting"}
    assert ids(state=pipeline.CLOSED) == {"won"}
    assert ids(state=pipeline.GUEST_REPLIED) == {"replied"}
    assert ids() == {"needs", "waiting", "won", "replied"}


def test_filter_by_kind_separates_leads_from_messages(tenant):
    _deal(tenant, "L1", kind="lead")
    _deal(tenant, "M1", kind="message", guest="Priya S.")
    assert {r["deal"]["item_id"]
            for r in pipeline.inbox_page(tenant, SITE, kind="lead")["rows"]} == {"L1"}
    assert {r["deal"]["item_id"]
            for r in pipeline.inbox_page(tenant, SITE, kind="message")["rows"]} == {"M1"}
    # An unrecognised kind must not silently filter everything out.
    assert len(pipeline.inbox_page(tenant, SITE, kind="nonsense")["rows"]) == 2


def test_filter_by_property(tenant):
    _deal(tenant, "A", unit="unit-1")
    _deal(tenant, "B", unit="unit-2")
    rows = pipeline.inbox_page(tenant, SITE, unit="unit-1")["rows"]
    assert {r["deal"]["item_id"] for r in rows} == {"A"}


def test_search_matches_the_guest_by_name(tenant):
    _deal(tenant, "A", guest="Emma M.")
    _deal(tenant, "B", guest="Priya S.")
    rows = pipeline.inbox_page(tenant, SITE, q="emma")["rows"]
    assert {r["deal"]["item_id"] for r in rows} == {"A"}
    # Case and partial matches both work; a miss returns nothing, not everything.
    assert len(pipeline.inbox_page(tenant, SITE, q="PRIY")["rows"]) == 1
    assert pipeline.inbox_page(tenant, SITE, q="nobody")["rows"] == []


def test_filters_combine(tenant):
    _deal(tenant, "A", guest="Emma M.", kind="lead", stage=pipeline.NEW)
    _deal(tenant, "B", guest="Emma M.", kind="message", stage=pipeline.NEW)
    rows = pipeline.inbox_page(tenant, SITE, q="emma", kind="message",
                               state=pipeline.NEEDS_YOU)["rows"]
    assert {r["deal"]["item_id"] for r in rows} == {"B"}


def test_a_search_that_looks_like_sql_is_just_a_search(tenant):
    _deal(tenant, "A", guest="Emma M.")
    assert pipeline.inbox_page(tenant, SITE, q="' OR 1=1 --")["rows"] == []
    assert len(pipeline.inbox_page(tenant, SITE)["rows"]) == 1, "table still there"


# --- counts ------------------------------------------------------------------


def test_counts_describe_the_whole_filtered_set_not_the_page(tenant):
    for i in range(7):
        _deal(tenant, f"n{i}", stage=pipeline.NEW)
    for i in range(3):
        _deal(tenant, f"c{i}", stage=pipeline.BOOKED, response={"status": "sent"})

    page = pipeline.inbox_page(tenant, SITE, per_page=2)
    assert len(page["rows"]) == 2, "the page is small"
    assert page["counts"][pipeline.NEEDS_YOU] == 7, "the count is not"
    assert page["counts"][pipeline.CLOSED] == 3
    assert page["counts"]["all"] == 10


def test_counts_respect_the_other_filters(tenant):
    _deal(tenant, "L1", kind="lead", stage=pipeline.NEW)
    _deal(tenant, "M1", kind="message", stage=pipeline.NEW)
    _deal(tenant, "M2", kind="message", stage=pipeline.BOOKED,
          response={"status": "sent"})
    counts = pipeline.inbox_page(tenant, SITE, kind="message")["counts"]
    assert counts[pipeline.NEEDS_YOU] == 1
    assert counts[pipeline.CLOSED] == 1
    assert counts["all"] == 2


def test_every_state_has_a_count_even_when_empty(tenant):
    """The tab bar renders all five; a missing key would be a template error."""
    _deal(tenant, "only", stage=pipeline.NEW)
    counts = pipeline.inbox_page(tenant, SITE)["counts"]
    assert set(pipeline.LEAD_STATES) <= set(counts)
    assert counts[pipeline.GUEST_REPLIED] == 0


# --- pagination --------------------------------------------------------------


def test_pagination_walks_the_whole_set_without_gaps_or_repeats(tenant):
    for i in range(12):
        _deal(tenant, f"d{i:02d}", inquiry=f"2026-08-{i + 1:02d}T09:00:00")

    seen, page = [], 1
    while True:
        result = pipeline.inbox_page(tenant, SITE, page=page, per_page=5)
        seen += [r["deal"]["item_id"] for r in result["rows"]]
        if page >= result["pages"]:
            break
        page += 1

    assert len(seen) == len(set(seen)) == 12, "no gaps, no duplicates"
    assert result["pages"] == 3
    assert result["total"] == 12


def test_newest_inquiry_comes_first(tenant):
    _deal(tenant, "older", inquiry="2026-08-01T09:00:00")
    _deal(tenant, "newer", inquiry="2026-08-09T09:00:00")
    rows = pipeline.inbox_page(tenant, SITE)["rows"]
    assert [r["deal"]["item_id"] for r in rows] == ["newer", "older"]


def test_pagination_totals_follow_the_state_filter(tenant):
    for i in range(6):
        _deal(tenant, f"n{i}", stage=pipeline.NEW)
    for i in range(2):
        _deal(tenant, f"c{i}", stage=pipeline.LOST)
    page = pipeline.inbox_page(tenant, SITE, state=pipeline.NEEDS_YOU, per_page=4)
    assert page["total"] == 6 and page["pages"] == 2
    assert all(r["state"] == pipeline.NEEDS_YOU for r in page["rows"])


def test_page_and_size_are_clamped_to_something_sane(tenant):
    _deal(tenant, "only")
    # A hand-typed ?page=0 or ?page=-3 must not produce a negative OFFSET.
    assert pipeline.inbox_page(tenant, SITE, page=0)["page"] == 1
    assert pipeline.inbox_page(tenant, SITE, page=-3)["page"] == 1
    # per_page is capped so a URL can't ask the server to render everything.
    assert pipeline.inbox_page(tenant, SITE,
                               per_page=10_000)["per_page"] == pipeline.MAX_PER_PAGE
    # An absent or empty ?per_page falls back to the default rather than
    # rendering a zero-row page.
    assert pipeline.inbox_page(tenant, SITE,
                               per_page=0)["per_page"] == pipeline.DEFAULT_PER_PAGE
    assert pipeline.inbox_page(tenant, SITE,
                               per_page=None)["per_page"] == pipeline.DEFAULT_PER_PAGE
    assert pipeline.inbox_page(tenant, SITE, per_page=-5)["per_page"] == 1


def test_a_page_past_the_end_is_empty_not_an_error(tenant):
    _deal(tenant, "only")
    page = pipeline.inbox_page(tenant, SITE, page=99)
    assert page["rows"] == [] and page["total"] == 1


# --- isolation ---------------------------------------------------------------


def test_one_tenants_inbox_never_shows_anothers(tenant):
    _deal(tenant, "mine")
    _deal("2", "theirs")
    rows = pipeline.inbox_page(tenant, SITE)["rows"]
    assert {r["deal"]["item_id"] for r in rows} == {"mine"}
    assert pipeline.inbox_page(tenant, SITE)["counts"]["all"] == 1
