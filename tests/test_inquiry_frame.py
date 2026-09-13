"""VEN-226: `inquiry_at` is written in one declared frame.

Both arms of `derive`'s `inquiry_at` write path now produce a server-frame
instant: the parsed listing-date arm converts property-local 09:00 into the
server frame (and clamps to now), and the fallback arm was always `_now()`.

Tests are grouped by acceptance criterion. Frozen clock; never TZ/tzset().
Item IDs are namespaced `q226_` to avoid collisions in a shared SQLite file.
"""
import ast
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import pipeline  # noqa: E402
import scheduler  # noqa: E402
import sequences  # noqa: E402
import storage  # noqa: E402

SITE = "furnishedfinder"
REF_DAY = date(2026, 3, 10)
AHEAD, BEHIND = 1, -1

CANDIDATE_ZONES = [
    "Pacific/Kiritimati",
    "Pacific/Auckland",
    "Asia/Tokyo",
    "Asia/Kolkata",
    "Europe/Berlin",
    "UTC",
    "America/New_York",
    "America/Los_Angeles",
    "Pacific/Honolulu",
    "Pacific/Midway",
]

# Deterministic UTC+14 zone for tests that need a known large offset.
KIRITIMATI = "Pacific/Kiritimati"


class _FrozenClock:
    @staticmethod
    def make(frozen: datetime):
        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return frozen
                return frozen.astimezone().astimezone(tz)
        return _DT


def _freeze(monkeypatch, frozen: datetime, *modules):
    dt = _FrozenClock.make(frozen)
    for m in modules:
        monkeypatch.setattr(m, "datetime", dt)


def _zone_where_property_is(direction: int, day: date = REF_DAY):
    """(zone, naive server instant, property date) with the property's date
    exactly `direction` days from the server's date.

    Computed from zoneinfo directly, not from scheduler, so the fixture does
    not move with any bug in the code under test.
    """
    from zoneinfo import ZoneInfo
    for zone in CANDIDATE_ZONES:
        tz = ZoneInfo(zone)
        for hour in range(24):
            for minute in (0, 30):
                naive = datetime.combine(day, time(hour, minute))
                prop = naive.astimezone().astimezone(tz)
                if (prop.date() - naive.date()).days == direction:
                    return zone, naive, prop.date()
    raise AssertionError(
        f"no candidate zone puts the property {direction:+d} day from the "
        f"server on {day}; zone table needs updating")


def _lead(item_id: str, listing: date, field: str = "received_at") -> dict:
    return {"id": item_id, "kind": "lead", "traveler": item_id,
            "title": f"Unit 1 | Washington, District of Columbia | {item_id}",
            field: listing.strftime("%B %d, %Y") if field == "received_at"
            else listing.strftime("%-m/%-d/%y")}


def _open(tenant_id: str, item: dict) -> dict:
    storage.filter_new(tenant_id, SITE, "lead", [item])
    pipeline.ensure(tenant_id, SITE, item, None)
    return pipeline.get(tenant_id, SITE, item["id"])


def _inquiry_at(tenant_id: str, item_id: str) -> str:
    return pipeline.get(tenant_id, SITE, item_id)["inquiry_at"]


@pytest.fixture()
def tenant(tmp_path, monkeypatch):
    import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "q226.db")
    tid = "1"
    config.save_settings(tid, host_name="Test Host", timezone="",
                         digest_enabled="0", digest_hour="18:00",
                         autopilot="0", check_times="09:00,16:00",
                         last_check_at="", last_digest_at="")
    return tid


# ---------------------------------------------------------------------------
# AC1 — one frame: parsed arm stores 09:00 property-local in the server frame
# ---------------------------------------------------------------------------

def test_ac1_parsed_arm_stores_09_in_server_frame(tenant, monkeypatch):
    """Parsed listing-date arm: 09:00 property-local, converted to server frame."""
    zone, server_now, prop_date = _zone_where_property_is(AHEAD)
    offset_h = (scheduler.local_now(zone if False else tenant,
                                    server_now) - server_now).total_seconds() / 3600
    config.save_settings(tenant, timezone=zone)
    _freeze(monkeypatch, server_now, pipeline, scheduler)

    listing = prop_date - timedelta(days=3)
    row = _open(tenant, _lead("q226_ac1_parsed", listing))
    stored = row["inquiry_at"]

    # Vacuity: the zone must produce a non-zero offset.
    local_now_dt = scheduler.local_now(tenant, server_now)
    assert local_now_dt.date() != server_now.date(), \
        f"vacuity: zone {zone!r} must differ by a day, got same date"

    expected = scheduler.server_naive(
        tenant, datetime.combine(listing, time(9, 0))
    ).isoformat(timespec="seconds")
    assert stored == expected, (zone, stored, expected)

    # Positive control: fallback arm (unparseable date) also in server frame.
    fallback_item = {"id": "q226_ac1_fallback", "kind": "lead", "traveler": "u",
                     "title": "Unit 1 | Washington, DC | u",
                     "received_at": "not a date"}
    storage.filter_new(tenant, SITE, "lead", [fallback_item])
    pipeline.ensure(tenant, SITE, fallback_item, None)
    fallback_stored = _inquiry_at(tenant, "q226_ac1_fallback")

    assert fallback_stored == server_now.isoformat(timespec="seconds"), fallback_stored
    # Both values are naive, T-separated, seconds precision — norm_ts handles both.
    for v in (stored, fallback_stored):
        assert len(v) == 19 and v[10] == "T", v
        assert datetime.fromisoformat(v).microsecond == 0, v


# ---------------------------------------------------------------------------
# AC2 — never in the future: three configurations
# ---------------------------------------------------------------------------

def test_ac2_not_in_future_property_ahead(tenant, monkeypatch):
    """Property ahead of server: 09:00 property-local is in the server's future."""
    zone, server_now, prop_date = _zone_where_property_is(AHEAD)
    config.save_settings(tenant, timezone=zone)
    _freeze(monkeypatch, server_now, pipeline, scheduler)

    row = _open(tenant, _lead("q226_ac2_ahead", prop_date, "received_at"))
    stored = datetime.fromisoformat(row["inquiry_at"])
    assert stored <= server_now, \
        f"inquiry_at {row['inquiry_at']!r} is in the future; server_now={server_now}"


def test_ac2_not_in_future_property_behind(tenant, monkeypatch):
    """Property behind server: clamp should not fire; timestamp is in the past."""
    zone, server_now, prop_date = _zone_where_property_is(BEHIND)
    config.save_settings(tenant, timezone=zone)
    _freeze(monkeypatch, server_now, pipeline, scheduler)

    row = _open(tenant, _lead("q226_ac2_behind", prop_date, "received_at"))
    stored = datetime.fromisoformat(row["inquiry_at"])
    assert stored <= server_now, \
        f"inquiry_at {row['inquiry_at']!r} is in the future; server_now={server_now}"


def test_ac2_not_in_future_no_timezone_before_0900(tenant, monkeypatch):
    """No property zone, server frozen at 03:00: clamp must fire without a zone.

    This is F2·0b: the 09:00 fiction is always in the future before 09:00 local,
    regardless of any timezone. Fails on base with `timezone=""`.
    """
    server_now = datetime(REF_DAY.year, REF_DAY.month, REF_DAY.day, 3, 0)
    _freeze(monkeypatch, server_now, pipeline, scheduler)
    # No timezone set (fixture default).

    row = _open(tenant, _lead("q226_ac2_nozone", server_now.date(), "received_at"))
    stored = datetime.fromisoformat(row["inquiry_at"])
    assert stored <= server_now, \
        f"inquiry_at {row['inquiry_at']!r} ahead of server_now={server_now} — no zone required"


# ---------------------------------------------------------------------------
# AC3 — same-day property date survives the gate
# ---------------------------------------------------------------------------

def test_ac3_same_day_survives_when_property_is_ahead(tenant, monkeypatch):
    """The filed Finding 1: `received` path used to discard the same-day date."""
    zone, server_now, prop_date = _zone_where_property_is(AHEAD)
    config.save_settings(tenant, timezone=zone)
    _freeze(monkeypatch, server_now, pipeline, scheduler)

    # `received_at` path — direct, has no gate.
    direct = pipeline.inquiry_date(
        _lead("q226_ac3_direct", prop_date, "received_at"))
    assert direct == prop_date.isoformat(), ("direct path", direct)

    # `received` path — the one that used to hit the server-date gate.
    gate = pipeline._inquiry_gate_today(tenant, server_now)
    rowish = pipeline.inquiry_date(
        _lead("q226_ac3_rowish", prop_date, "received"), today=gate)
    assert rowish == prop_date.isoformat(), \
        f"same-day property date discarded: zone={zone} server={server_now.date()} prop={prop_date}"

    # The two item paths now agree about the same date.
    row_direct = _open(tenant, _lead("q226_ac3_d", prop_date, "received_at"))
    row_rowish = _open(tenant, _lead("q226_ac3_r", prop_date, "received"))
    assert row_direct["inquiry_at"] == row_rowish["inquiry_at"], \
        (row_direct["inquiry_at"], row_rowish["inquiry_at"])


def test_ac3_far_future_date_still_rejected(tenant, monkeypatch):
    """Positive control: a move-out date 60 days out is still rejected."""
    zone, server_now, prop_date = _zone_where_property_is(AHEAD)
    config.save_settings(tenant, timezone=zone)
    _freeze(monkeypatch, server_now, pipeline, scheduler)

    gate = pipeline._inquiry_gate_today(tenant, server_now)
    far = pipeline.inquiry_date(
        _lead("q226_ac3_far", prop_date + timedelta(days=60), "received"), today=gate)
    assert far is None, f"60-day-future date should be rejected, got {far!r}"


# ---------------------------------------------------------------------------
# AC4 — age chip: a 12.5h-old lead is deal--hot, not deal--fresh
# ---------------------------------------------------------------------------

def test_ac4_age_chip_reflects_true_elapsed_time(tenant, monkeypatch):
    """UTC+14, server 07:30, property 21:30: lead is 12.5 property-hours old.

    On base the stamp is in the server's future: age_hours=-1.5h, chip='1m',
    class=deal--fresh. After VEN-226: +12.5h, '12h', deal--hot.
    """
    server_now = datetime(2026, 3, 11, 7, 30)
    config.save_settings(tenant, timezone=KIRITIMATI)
    _freeze(monkeypatch, server_now, pipeline, scheduler)

    prop_now = scheduler.local_now(tenant, server_now)
    offset_h = (prop_now - server_now).total_seconds() / 3600
    listing = prop_now.date()
    assert listing == server_now.date(), \
        "vacuity: both frames must be on the same date so the skew is pure offset"
    assert offset_h > 0, f"vacuity: UTC+14 should give positive offset, got {offset_h}"

    row = _open(tenant, _lead("q226_ac4", listing, "received_at"))
    true_age = (prop_now - datetime.combine(listing, time(9, 0))).total_seconds() / 3600
    shown = pipeline.age_hours(row["inquiry_at"])
    css = ("deal--hot" if shown and shown > 12 else
           "deal--warm" if shown and shown > 2 else "deal--fresh")

    assert shown is not None and abs(shown - true_age) < 0.02, \
        f"age should be {true_age:.1f}h, got {shown}"
    assert css == "deal--hot", \
        f"12.5h-old lead should be deal--hot, got {css!r} (age={shown:.2f}h)"

    # Positive control: property BEHIND the server — age still non-negative.
    zone_b, server_now_b, prop_date_b = _zone_where_property_is(BEHIND)
    config.save_settings(tenant, timezone=zone_b)
    _freeze(monkeypatch, server_now_b, pipeline, scheduler)
    row_b = _open(tenant, _lead("q226_ac4b", prop_date_b, "received_at"))
    shown_b = pipeline.age_hours(row_b["inquiry_at"])
    assert shown_b is not None and shown_b >= 0, \
        f"age should be non-negative in BEHIND direction, got {shown_b}"


# ---------------------------------------------------------------------------
# AC5 — response-time metric counts the fast replies
# ---------------------------------------------------------------------------

def test_ac5_metrics_counts_fast_replies(tenant, monkeypatch):
    """A reply faster than the zone offset must be included in median_response."""
    zone, server_now, prop_date = _zone_where_property_is(AHEAD)
    config.save_settings(tenant, timezone=zone)
    _freeze(monkeypatch, server_now, pipeline, scheduler)

    # Fast lead: same-day listing, replied at server_now.
    fast = _open(tenant, _lead("q226_ac5_fast", prop_date, "received_at"))
    # Slow lead: 3 days ago, also replied at server_now.
    slow = _open(tenant, _lead("q226_ac5_slow",
                               prop_date - timedelta(days=3), "received_at"))

    reply = server_now.isoformat(timespec="seconds")
    for item_id in ("q226_ac5_fast", "q226_ac5_slow"):
        pipeline.update(tenant, SITE, item_id, first_reply_at=reply)

    deals = pipeline.all_deals(tenant, SITE)
    m = pipeline.metrics(deals, {})

    counted = sum(
        1 for d in deals
        if pipeline._to_dt(d.get("first_reply_at"))
        and pipeline._to_dt(d.get("inquiry_at"))
        and pipeline._to_dt(d["first_reply_at"]) >= pipeline._to_dt(d["inquiry_at"])
    )
    assert counted == 2, \
        f"both reply pairs should be counted; got {counted} of 2"
    assert m["median_response"] is not None

    # Positive control: a reply genuinely before its inquiry is excluded.
    impossible = _open(tenant, _lead("q226_ac5_impossible",
                                     prop_date - timedelta(days=2), "received_at"))
    past_reply = (server_now - timedelta(days=10)).isoformat(timespec="seconds")
    pipeline.update(tenant, SITE, "q226_ac5_impossible", first_reply_at=past_reply)
    deals2 = pipeline.all_deals(tenant, SITE)
    exc = sum(
        1 for d in deals2
        if d["item_id"] == "q226_ac5_impossible"
        and pipeline._to_dt(d.get("first_reply_at"))
        and pipeline._to_dt(d.get("inquiry_at"))
        and pipeline._to_dt(d["first_reply_at"]) >= pipeline._to_dt(d["inquiry_at"])
    )
    assert exc == 0, "a reply before inquiry_at must not be counted"


# ---------------------------------------------------------------------------
# AC6 — the first step is not scheduled before its own trigger
# ---------------------------------------------------------------------------

def test_ac6_intro_step_not_scheduled_into_future(tenant, monkeypatch):
    """`sequences.schedule()` for the PRESALE intro step yields due_at <= now."""
    zone, server_now, prop_date = _zone_where_property_is(AHEAD)
    config.save_settings(tenant, timezone=zone)
    _freeze(monkeypatch, server_now, pipeline, scheduler, sequences)

    row = _open(tenant, _lead("q226_ac6", prop_date, "received_at"))
    anchor = sequences._anchor_dt(row, sequences.A_INQUIRY)
    due, step_id = sequences.schedule(row)

    assert anchor is not None, "anchor must resolve"
    late_h = (anchor - server_now).total_seconds() / 3600
    assert late_h <= 0, \
        (f"intro anchored {late_h:+.1f}h in the future; "
         f"inquiry_at={row['inquiry_at']!r} server_now={server_now}")
    assert step_id == "intro", f"expected presale intro, got {step_id!r}"


# ---------------------------------------------------------------------------
# AC7 — backfill converges and heals
# ---------------------------------------------------------------------------

def test_ac7a_new_writer_row_does_not_churn(tenant, monkeypatch):
    """Steady state: 3 backfill passes on a new-writer row write zero times."""
    zone, server_now, prop_date = _zone_where_property_is(AHEAD)
    config.save_settings(tenant, timezone=zone)
    _freeze(monkeypatch, server_now, pipeline, scheduler)

    item = _lead("q226_ac7a", prop_date, "received_at")
    _open(tenant, item)
    items = {"q226_ac7a": item}

    writes = []
    real_ensure = pipeline.ensure
    monkeypatch.setattr(
        pipeline, "ensure",
        lambda *a, **k: (writes.append(a[2].get("id")), real_ensure(*a, **k))[1]
    )
    for _ in range(3):
        pipeline.backfill(tenant, SITE, items, {})

    assert writes == [], \
        f"new-writer row should not be re-derived; got {len(writes)} writes"


def test_ac7b_legacy_row_heals_exactly_once(tenant, monkeypatch):
    """A row SQL-stamped in the legacy property frame is re-derived once, then converges.

    The heal triggers via `_property_date(stored) != truth`: the legacy stamp is the
    property's `<date>T09:00:00`, stored as if server-frame. `_property_date` reads
    it as a server instant, converts to property-local, and gets a date one day
    earlier (because UTC-11 is 11h behind; 09:00 server − 11h = 22:00 the previous
    property day). That differs from truth = the listing date, so re-derive fires.

    UTC-11 (Pacific/Midway) is required: zones with offset < 9h don't cross midnight
    (09:00 server − 5h = 04:00 same property day), so only > 9h offsets cause the
    day shift this test exercises.
    """
    # Use Pacific/Midway (UTC-11): the 11h offset reliably crosses midnight for
    # a legacy 09:00 stamp (09:00 server − 11h = 22:00 prev property day). The
    # scanner finds the earliest hour on REF_DAY where Midway is one day behind.
    from zoneinfo import ZoneInfo
    zone = "Pacific/Midway"
    tz = ZoneInfo(zone)
    zone_candidates = [zone]
    _BEHIND_ONLY = [-1]
    server_now, prop_date = None, None
    for hour in range(24):
        for minute in (0, 30):
            if hour == 0 and minute == 0:
                continue  # 00:00 puts legacy 09:00 stamp in the future
            naive = datetime.combine(REF_DAY, time(hour, minute))
            prop = naive.astimezone().astimezone(tz)
            if (prop.date() - naive.date()).days == -1:
                server_now, prop_date = naive, prop.date()
                break
        if server_now is not None:
            break
    assert server_now is not None, \
        "vacuity: could not find a server time where Midway is one day behind"
    assert prop_date == server_now.date() - timedelta(days=1), \
        f"vacuity: prop_date={prop_date} should be server_date-1={server_now.date()-timedelta(1)}"

    config.save_settings(tenant, timezone=zone)
    _freeze(monkeypatch, server_now, pipeline, scheduler)

    listing = prop_date - timedelta(days=3)
    item = _lead("q226_ac7b", listing, "received_at")
    _open(tenant, item)

    # SQL-stamp in the legacy property frame (what the pre-VEN-226 writer produced).
    legacy_stamp = f"{listing.isoformat()}T09:00:00"
    with pipeline._conn() as c:
        c.execute(
            "UPDATE deals SET inquiry_at=? WHERE tenant_id=? AND site=? AND item_id=?",
            (legacy_stamp, tenant, SITE, "q226_ac7b"))

    # Sanity: _property_date of the legacy stamp gives a different date.
    assert pipeline._property_date(legacy_stamp, tenant) != listing.isoformat(), \
        "_property_date of legacy stamp must differ from listing date — this is what triggers heal"

    items = {"q226_ac7b": item}
    writes = []
    real_ensure = pipeline.ensure
    monkeypatch.setattr(
        pipeline, "ensure",
        lambda *a, **k: (writes.append(a[2].get("id")), real_ensure(*a, **k))[1]
    )

    # Pass 1: legacy row differs → re-derive.
    pipeline.backfill(tenant, SITE, items, {})
    assert len(writes) == 1, f"legacy row must be healed on pass 1; got {writes}"

    # Passes 2 and 3: now converges.
    pipeline.backfill(tenant, SITE, items, {})
    pipeline.backfill(tenant, SITE, items, {})
    assert len(writes) == 1, \
        f"after heal, no further writes expected; total={len(writes)}"


def test_ac7c_unparseable_item_never_rederives(tenant, monkeypatch):
    """An item with no parseable date is never re-derived by backfill."""
    zone, server_now, _ = _zone_where_property_is(AHEAD)
    config.save_settings(tenant, timezone=zone)
    _freeze(monkeypatch, server_now, pipeline, scheduler)

    item = {"id": "q226_ac7c", "kind": "lead", "traveler": "u",
            "title": "Unit 1 | Washington, DC | u",
            "received_at": "not a date"}
    storage.filter_new(tenant, SITE, "lead", [item])
    pipeline.ensure(tenant, SITE, item, None)

    items = {"q226_ac7c": item}
    writes = []
    real_ensure = pipeline.ensure
    monkeypatch.setattr(
        pipeline, "ensure",
        lambda *a, **k: (writes.append(a[2].get("id")), real_ensure(*a, **k))[1]
    )
    for _ in range(3):
        pipeline.backfill(tenant, SITE, items, {})

    assert writes == [], \
        f"item with no parseable date must never re-derive; got {writes}"


# ---------------------------------------------------------------------------
# AC7d — the fourth row class: a listing date AHEAD of the property's calendar
#
# AC7a-c describe the three write paths. They do not reach the case where
# `_inquiry_stamp`'s clamp fires on a *future* listing date: the stored stamp
# then lands on the property's today rather than on the listing date, so a
# predicate that compares the stored stamp's property date against raw `truth`
# is unequal forever and re-derives on every dashboard load. `received_at`
# bypasses `inquiry_date`'s future gate entirely, so this class is reachable
# with any date ahead of the property; `received` reaches it whenever the
# property's calendar is a day behind the server's.
# ---------------------------------------------------------------------------

def _count_backfill_writes(tenant_id, items, passes=3) -> list:
    """Item ids re-derived by `passes` steady-state backfill passes.

    Patches and restores by hand rather than through `monkeypatch`: this is
    called more than once per test, and `monkeypatch.undo()` would also revert
    the fixture's `db.DB_PATH` and the frozen clock.
    """
    writes = []
    real_ensure = pipeline.ensure
    pipeline.ensure = (
        lambda *a, **k: (writes.append(a[2].get("id")), real_ensure(*a, **k))[1])
    try:
        for _ in range(passes):
            pipeline.backfill(tenant_id, SITE, items, {})
    finally:
        pipeline.ensure = real_ensure
    return writes


@pytest.mark.parametrize("field", ["received_at", "received"])
def test_ac7d_listing_ahead_of_property_converges(tenant, monkeypatch, field):
    """A listing date one day ahead of the property's calendar does not churn."""
    zone, server_now, prop_date = _zone_where_property_is(BEHIND)
    config.save_settings(tenant, timezone=zone)
    _freeze(monkeypatch, server_now, pipeline, scheduler)

    listing = prop_date + timedelta(days=1)
    item_id = f"q226_ac7d_{field}"
    item = _lead(item_id, listing, field)
    _open(tenant, item)
    stored = _inquiry_at(tenant, item_id)

    # Vacuity 1: the date must actually survive the gate, or nothing is tested.
    gate = pipeline._inquiry_gate_today(tenant, server_now)
    assert pipeline.inquiry_date(item, today=gate) == listing.isoformat(), \
        f"vacuity: {field} listing {listing} must survive the gate {gate}"
    # Vacuity 2: the clamp must have fired — that is the whole point of the
    # class. If the stamp still lands on the listing date, the old predicate
    # would have converged too and this test proves nothing.
    assert pipeline._property_date(stored, tenant) != listing.isoformat(), \
        (f"vacuity: clamp must move the stamp off the listing date; "
         f"stored={stored} property_date={pipeline._property_date(stored, tenant)}")

    assert _count_backfill_writes(tenant, {item_id: item}) == [], \
        "a clamped future listing date must converge, not re-derive every load"

    # Positive control, same deal: a genuinely wrong stamp still heals.
    with pipeline._conn() as c:
        c.execute(
            "UPDATE deals SET inquiry_at=? WHERE tenant_id=? AND site=? AND item_id=?",
            ((server_now - timedelta(days=40)).isoformat(timespec="seconds"),
             tenant, SITE, item_id))
    assert _count_backfill_writes(tenant, {item_id: item}) == [item_id], \
        "control: a stamp on the wrong property day must still be healed once"


@pytest.mark.parametrize("zone", ["Pacific/Honolulu", "Pacific/Midway",
                                  "Pacific/Kiritimati", "America/New_York"])
@pytest.mark.parametrize("offset", [0, 1])
def test_ac7d_grid_no_zone_hour_cell_churns(tenant, monkeypatch, zone, offset):
    """Grid: no (zone, server hour, listing offset) cell re-derives in steady state.

    The regression this pins is narrow — the review found 6 churning cells at
    Honolulu/Midway around server 09:30-10:30 — so the grid sweeps the hours
    rather than picking one, and counts every failing cell instead of stopping
    at the first. `checked` is the positive control: a grid where every cell
    was vacuous (gate rejected the date) would otherwise pass silently.
    """
    config.save_settings(tenant, timezone=zone)
    churn, checked = [], 0
    for hour in (0, 9, 10, 13, 21):
        for field in ("received_at", "received"):
            server_now = datetime.combine(REF_DAY, time(hour, 30))
            _freeze(monkeypatch, server_now, pipeline, scheduler)
            prop_date = scheduler.local_now(tenant, server_now).date()
            listing = prop_date + timedelta(days=offset)
            item_id = f"q226_g_{zone[-4:]}_{hour}_{field}_{offset}"
            item = _lead(item_id, listing, field)
            gate = pipeline._inquiry_gate_today(tenant, server_now)
            if pipeline.inquiry_date(item, today=gate) is None:
                continue  # gate rejected it: no truth, nothing to converge on
            checked += 1
            _open(tenant, item)
            writes = _count_backfill_writes(tenant, {item_id: item})
            if writes:
                churn.append((zone, hour, field, listing.isoformat(), len(writes)))
            _freeze(monkeypatch, server_now, pipeline, scheduler)

    assert checked >= 4, \
        f"vacuity: grid for {zone} offset +{offset} only exercised {checked} cells"
    assert churn == [], f"cells re-deriving in steady state: {churn}"


# ---------------------------------------------------------------------------
# AC9 — backfill's settings lookups are fixed per pass, not per deal
#
# `dashboard._board`'s contract is "assembled in a fixed number of queries", and
# `scheduler.tz_for` reaches `config.get_settings`, which has no cache and opens
# its own connection. Resolving the zone inside the per-item helper made a
# steady-state pass linear in the deal count (measured: 300 deals -> 600 calls).
# ---------------------------------------------------------------------------

def _settings_calls_for(tenant_id, n_deals, server_now) -> int:
    items = {}
    for i in range(n_deals):
        item_id = f"q226_ac9_{n_deals}_{i}"
        item = _lead(item_id, server_now.date() - timedelta(days=5), "received_at")
        _open(tenant_id, item)
        items[item_id] = item

    calls = []
    real = config.get_settings
    # Hand-rolled patch/restore, not `monkeypatch`: see `_count_backfill_writes`.
    config.get_settings = lambda *a, **k: (calls.append(a[:1]), real(*a, **k))[1]
    try:
        created = pipeline.backfill(tenant_id, SITE, items, {})
    finally:
        config.get_settings = real
    assert created == 0, f"steady state expected; backfill created {created} deals"
    return len(calls)


def test_ac9_backfill_settings_lookups_do_not_scale_with_deal_count(tenant, monkeypatch):
    """A steady-state backfill pass costs the same settings lookups at 1 deal and at 20."""
    zone, server_now, _ = _zone_where_property_is(AHEAD)
    config.save_settings(tenant, timezone=zone)
    _freeze(monkeypatch, server_now, pipeline, scheduler)

    one = _settings_calls_for(tenant, 1, server_now)
    twenty = _settings_calls_for(tenant, 20, server_now)

    assert one == twenty, (
        f"backfill's settings lookups must not scale with the deal count: "
        f"1 deal -> {one} calls, 20 deals -> {twenty} calls "
        f"(+{(twenty - one) / 19:.1f} per extra deal)")


# ---------------------------------------------------------------------------
# AC8 — AST: every in-repo call to pipeline.derive passes tenant_id
# ---------------------------------------------------------------------------

def test_ac8_all_derive_calls_pass_tenant_id():
    """Every call to `pipeline.derive` in the repo passes `tenant_id`.

    `tenant_id=None` keeps the old behaviour for out-of-tree callers, but in-repo
    callers must opt in; a missing argument silently restores the old frame.
    """
    repo_root = Path(__file__).resolve().parent.parent
    violations = []
    for py_file in repo_root.glob("**/*.py"):
        # Skip test files and the module itself.
        rel = py_file.relative_to(repo_root)
        if rel.parts[0] in ("tests",) or py_file.name == "pipeline.py":
            continue
        try:
            tree = ast.parse(py_file.read_text(), filename=str(py_file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_derive = (
                (isinstance(func, ast.Attribute) and func.attr == "derive"
                 and isinstance(func.value, ast.Name) and func.value.id == "pipeline")
                or (isinstance(func, ast.Name) and func.attr == "derive"
                    if hasattr(func, "attr") else False)
            )
            if not is_derive:
                continue
            has_tenant_id = any(
                kw.arg == "tenant_id" for kw in node.keywords
            )
            if not has_tenant_id:
                violations.append(f"{rel}:{node.lineno}")

    assert violations == [], (
        "These in-repo calls to pipeline.derive are missing `tenant_id=...`:\n"
        + "\n".join(violations)
    )
