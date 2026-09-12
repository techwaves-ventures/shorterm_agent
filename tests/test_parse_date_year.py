"""The year `parse_date` fills in when the site didn't state one.

The message list renders dates as 'Jul. 18' with no year at all. That is the
only shape in the product that arrives half-specified, and the missing half is
the one every urgency clock is built on: `inquiry_at`, the needs-action window,
and the abandonment close all read it. Across New Year the wrong guess is off by
twelve months, so these tests are written around a frozen turn of the year
rather than around today — the defect is invisible for eleven months a year.

Every case pins the server clock rather than passing the `today` argument, so
each one is a claim about behaviour that an older build can fail — the argument
is a seam added alongside the fix, and a test that only exercised it would be
red on the old code for the wrong reason. `test_the_reference_date_...` is the
one test about the seam itself.

Kept in its own file because the reference date has to be pinned per case, and
the shared `tenant` fixture in test_agent_lifecycle.py deliberately anchors
everything to *now*.
"""
import os
import tempfile
from datetime import datetime as _real_datetime

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix="-parse-date-year.db"))
os.environ.setdefault("SECRET_KEY", "test-secret")

import pipeline  # noqa: E402

SITE = "furnishedfinder"


class _FrozenDatetime:
    """`datetime` with `now()` pinned; construction and parsing stay real."""

    def __init__(self, instant):
        self._instant = instant

    def now(self, tz=None):
        return self._instant

    def __getattr__(self, name):
        return getattr(_real_datetime, name)

    def __call__(self, *a, **kw):
        return _real_datetime(*a, **kw)


@pytest.fixture()
def freeze(monkeypatch):
    """Pin the server instant `pipeline` reads.

    Patches the name `pipeline` holds, not `datetime.datetime` itself — the
    module did `from datetime import datetime`, so that binding is the input
    the code under test actually reads.
    """
    def _freeze(day, clock="12:00:00"):
        monkeypatch.setattr(
            pipeline, "datetime",
            _FrozenDatetime(_real_datetime.fromisoformat(f"{day}T{clock}")))
    return _freeze


@pytest.fixture()
def parse_on(freeze):
    """`parse_date(value)` as read on `day`, with the server clock pinned there."""
    def _parse(value, day):
        freeze(day)
        return pipeline.parse_date(value)
    return _parse


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    import config
    import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    tid = "ven230"
    config.save_settings(tid, host_name="Test Host", timezone="America/New_York",
                         digest_enabled="0", autopilot="0")
    return tid


def _message(item_id: str, date: str) -> dict:
    """A scraped message-list entry, as furnishedfinder._extract_messages emits it."""
    return {"id": item_id, "kind": "message", "sender": "Jamie R.",
            "date": date, "title": f"Jamie R. ({date})", "body": "Is it still open?"}


# --- The year is chosen by distance, not by the calendar the server is on ----

def test_a_december_date_read_in_january_keeps_its_december(parse_on):
    """Filling in the server's year put this lead eleven months in the FUTURE."""
    assert parse_on("Dec. 15", "2027-01-03") == "2026-12-15"


def test_a_new_year_date_is_not_filed_under_the_year_that_just_ended(parse_on):
    """The site renders the property's calendar day, which can already be
    tomorrow from the server's point of view. Filling in the server's year
    aged this message by 364 days."""
    assert parse_on("Jan. 1", "2026-12-31") == "2027-01-01"


def test_a_mid_year_date_still_takes_the_current_year(parse_on):
    """The control for both of the above: eleven months a year nothing moves."""
    assert parse_on("Jul. 18", "2026-07-20") == "2026-07-18"


def test_a_date_a_few_days_ahead_still_takes_the_current_year(parse_on):
    """A rule of 'never in the future' would send this one back a full year."""
    assert parse_on("Jul. 22", "2026-07-20") == "2026-07-22"


def test_a_stated_year_is_never_second_guessed(parse_on):
    """Only the yearless shape gets a guess; the other two shapes state it."""
    assert parse_on("Jul. 18, 2024", "2026-07-20") == "2024-07-18"
    assert parse_on("July 18, 2024", "2026-07-20") == "2024-07-18"
    assert parse_on("7/18/24", "2026-07-20") == "2024-07-18"


def test_the_reference_date_can_be_supplied_by_the_caller(freeze):
    """`today` is a seam for a caller that knows the property's own calendar
    day; left off, it is the server's.

    The reference days here are chosen to give the same input three *different*
    answers, so an implementation that accepted the argument and then ignored it
    would fail. An earlier draft picked days that happened to agree, and the
    ignore-the-argument mutant survived it.
    """
    freeze("2026-07-20")
    assert pipeline.parse_date("Dec. 15") == "2026-12-15"
    assert pipeline.parse_date("Dec. 15", today=None) == "2026-12-15"
    assert pipeline.parse_date("Dec. 15", today="2028-09-01") == "2028-12-15"
    assert pipeline.parse_date("Dec. 15", today="2025-02-01") == "2024-12-15"


# --- Dates that cannot be placed stay unplaced -------------------------------

def test_a_leap_day_with_no_leap_year_in_reach_is_still_unparseable(parse_on):
    """2027-02-29 does not exist and 2028-02-29 is a year away. A guess that far
    out is worse than the None the caller already knows how to handle."""
    assert parse_on("Feb. 29", "2027-03-01") is None


def test_a_leap_day_within_reach_does_parse(parse_on):
    """The positive control for the bound above — it rejects by distance, not
    by refusing leap days."""
    assert parse_on("Feb. 29", "2028-01-15") == "2028-02-29"


def test_an_exact_tie_across_a_leap_year_resolves_to_the_earlier_year(parse_on):
    """2027-03-01 and 2028-03-01 are 366 days apart, so from 2027-08-31 both are
    exactly 183 away. The only caller that reaches this code is reading dates
    the guest has already written, so the past wins."""
    assert parse_on("Mar. 1", "2027-08-31") == "2027-03-01"


def test_nothing_that_did_not_parse_before_starts_parsing(parse_on):
    """The guess is a choice between real calendar dates, not a looser parse."""
    for junk in ("Feb. 30", "Foo. 3", "a week earlier?", "2026", "12/32/26", "", None):
        assert parse_on(junk, "2026-12-31") is None


# --- What it costs the owner when the year is wrong --------------------------

def test_a_december_lead_read_in_january_is_stamped_three_weeks_old(freeze):
    """End to end: the wrong year made the future gate reject the date, and the
    deal fell back to the scrape instant — a three-week-old lead shown as new."""
    freeze("2027-01-03")
    fields = pipeline.derive(_message("dec-lead", "Dec. 15"), None)
    assert fields["inquiry_at"] == "2026-12-15T09:00:00"
    assert pipeline.humanize_age(fields["inquiry_at"]) == "19d"


def test_a_yearless_received_at_cannot_stamp_a_lead_in_the_future(freeze):
    """`inquiry_date` trusts `received_at` outright — the future gate guards only
    the row-derived value below it. `sites/ff_email._only_if_a_stamp` is a shape
    check, not a parse, so a yearless "Dec 15" in a notification email reaches
    that trusted path.

    Filled with the server's year in January, it landed eleven months AHEAD, and
    a future `inquiry_at` never ages: the chip reads '1m' forever, the deal never
    leaves the needs-action queue, and `_is_abandoned` can never fire on it.
    """
    freeze("2027-01-03")
    fields = pipeline.derive(
        {"id": "email-lead", "kind": "lead", "traveler": "Jamie R.",
         "received_at": "Dec 15"}, None)
    assert fields["inquiry_at"] == "2026-12-15T09:00:00"
    assert pipeline.age_hours(fields["inquiry_at"]) > 0
    assert pipeline.humanize_age(fields["inquiry_at"]) == "19d"


def test_a_new_year_message_is_not_auto_closed_as_abandoned(freeze, tenant):
    """The whole path the scheduler runs. A message that arrived hours ago was
    stamped 364 days old, which is past STALE_CLOSE_DAYS, so `advance_lifecycle`
    closed it to `lost` and the guest was never answered.

    The stale deal alongside it is the positive control: this asserts the close
    did not fire on the new lead, which would also pass if the close had stopped
    firing at all.
    """
    freeze("2026-12-31")
    pipeline.ensure(tenant, SITE, _message("nye", "Jan. 1"))
    pipeline.ensure(tenant, SITE, _message("cold", "Nov. 1"))

    moved = pipeline.advance_lifecycle(tenant, SITE)

    fresh = pipeline.get(tenant, SITE, "nye")
    assert fresh["inquiry_at"] == "2026-12-31T12:00:00"
    assert fresh["stage"] == "new"
    assert fresh["closed_reason"] is None

    cold = pipeline.get(tenant, SITE, "cold")
    assert cold["inquiry_at"] == "2026-11-01T09:00:00"
    assert cold["stage"] == "lost"
    assert cold["closed_reason"] == f"No reply for {pipeline.STALE_CLOSE_DAYS} days"
    assert moved["lost"] == 1
