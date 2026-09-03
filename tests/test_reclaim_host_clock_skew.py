"""VEN-208: a host whose clock runs behind writes a past-dated sending_at.

A host whose Python clock is N seconds behind writes ``sending_at`` as
"N seconds ago" via the old ``_now_utc()`` call. A correctly-clocked reclaimer
measures age = N + real_age, which past 900 s hands the row to a second drainer
while the first is still in flight — one send becomes two.

Fix: source ``sending_at`` AND the comparison cutoff from the database's clock
via ``db.utc_now_sql()`` / ``db.utc_now(conn)``. Both sides of every age
comparison share the same clock, so the writing host's Python-clock error
cancels out.

Test seam
---------
Patch ``db.utc_now_sql`` to return a SQL literal for a controlled instant, and
``db.utc_now`` to return that instant as a datetime.  On the **fix** code,
``set_status`` calls ``db.utc_now_sql()`` — so the stamp is what the pin says.
On the **base** code, ``set_status`` calls ``_now_utc()`` (the Python clock) —
the pin is ignored and the stamp is real-now.

Discriminating tests (fail on base, pass on fix)
------------------------------------------------
* ``test_stamp_uses_db_clock``       — stamp must equal the pinned DB instant,
                                        not the real Python clock.
* ``test_genuinely_stale_send_is_reclaimed`` — write at T0-901 s, reclaim at T0.
                                        On fix: age = 901 s → requeued.
                                        On base: stamp = real-now, age ≈ 0 → not requeued.

Positive guards (pass on both, required by the plan's criterion 3)
------------------------------------------------------------------
* ``test_live_send_not_requeued``    — write at T0, reclaim at T0+1 s; age = 1 s.
* ``test_stale_boundary``            — stamp exactly MAX_AGE old; not yet past threshold.

Postgres is mandatory for the writer-skew cases. SQLite executes both app and
DB on the same host/process (no second clock to be wrong) and never runs the
Postgres branch of ``utc_now_sql``.  A positive control asserts the PG fixture
is actually on Postgres so a fallback-to-SQLite cannot pass silently.
"""
import importlib
import os
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("SECRET_KEY", "test-secret")

PG_HOST = "/var/run/postgresql"
SITE = "furnishedfinder"
MAX_AGE = 900  # must match outbox.MAX_SEND_AGE_SECONDS default


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pg_url():
    import shutil
    if not (os.path.exists(PG_HOST) and shutil.which("createdb")):
        return None
    name = f"ven208_test_{uuid.uuid4().hex[:12]}"
    try:
        subprocess.run(["createdb", "-h", PG_HOST, name],
                       check=True, capture_output=True, timeout=30)
    except Exception:
        return None
    return name, f"postgresql://@/{name}?host={PG_HOST}"


def _dropdb(name):
    try:
        subprocess.run(["dropdb", "-h", PG_HOST, "--force", name],
                       capture_output=True, timeout=30)
    except Exception:
        pass


def _pin_db_clock(at: datetime):
    """Make the db helpers return a fixed instant (fix code reads these)."""
    import db
    stamp = at.isoformat(timespec="seconds")
    db.utc_now_sql = lambda: f"'{stamp}'"
    db.utc_now = lambda conn: at


def _unpin_db_clock():
    import db
    importlib.reload(db)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def pg_outbox(monkeypatch):
    """outbox bound to a fresh throwaway Postgres, reloaded fresh."""
    made = _pg_url()
    if made is None:
        pytest.skip("no local Postgres; writer-skew tests are UNVERIFIED here")
    name, url = made
    monkeypatch.setenv("DATABASE_URL", url)

    import db
    importlib.reload(db)
    import outbox
    importlib.reload(outbox)

    with outbox._conn() as c:
        assert c.pg, "pg_outbox fixture must be on Postgres"

    yield outbox

    _unpin_db_clock()
    _dropdb(name)
    importlib.reload(db)
    importlib.reload(outbox)


@pytest.fixture()
def sqlite_outbox(monkeypatch):
    """Fresh SQLite outbox (no Postgres required)."""
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("SQLITE_PATH", path)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    import db
    importlib.reload(db)
    import outbox
    importlib.reload(outbox)

    yield outbox

    _unpin_db_clock()
    importlib.reload(db)
    importlib.reload(outbox)


# ---------------------------------------------------------------------------
# Positive control: confirm PG is reachable (silent skip = UNVERIFIED)
# ---------------------------------------------------------------------------

def test_postgres_fixture_is_reachable(pg_outbox):
    with pg_outbox._conn() as c:
        assert c.pg, "pg_outbox is not actually on Postgres"


# ---------------------------------------------------------------------------
# Criterion 4 (discriminating) — stamp does NOT follow the Python clock
#
# Fix: set_status uses db.utc_now_sql() → stamp = pinned literal.
# Base: set_status uses _now_utc() = datetime.now(tz.utc) → stamp = real-now.
# ---------------------------------------------------------------------------

def test_stamp_uses_db_clock_not_python_clock(pg_outbox):
    """sending_at must equal the DB-clock instant, not the Python process clock.

    Fails on base: _now_utc() returns real-now which differs from T_YESTERDAY.
    Passes on fix: set_status writes db.utc_now_sql() = the pinned literal.
    """
    ob = pg_outbox
    T_YESTERDAY = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    _pin_db_clock(T_YESTERDAY)

    msg = ob.add("t1", SITE, "item_clock", sequence="presale", step_id="s",
                 step_label="S", body="b", auto=False)
    ob.set_status(msg["id"], ob.SENDING)
    row = ob.get(msg["id"])

    assert row["sending_at"] is not None, "sending_at must be set"
    stamp = datetime.fromisoformat(row["sending_at"])
    expected = T_YESTERDAY.replace(microsecond=0)
    diff = abs((stamp.replace(tzinfo=timezone.utc) - expected).total_seconds())
    assert diff < 2, (
        f"stamp {stamp!r} does not match pinned DB clock {expected!r}; "
        "set_status is still reading the Python clock (base code behaviour)"
    )


# ---------------------------------------------------------------------------
# Criterion 3 (discriminating) — a genuinely crashed send IS reclaimed
#
# Write at T0−901 s (DB pin), reclaim at T0 (DB pin).
# Fix:  stamp = T0−901, now = T0, age = 901 s → requeued ✓
# Base: stamp = real-now (pin ignored), now = real-clock, age ≈ 0 s → not requeued
#       → test FAILS on base, as required.
# ---------------------------------------------------------------------------

def test_genuinely_stale_send_is_reclaimed(pg_outbox):
    """A send whose stamp is MAX_AGE+1 s old must be requeued on the first pass.

    This is the criterion that blocks the wrong fix: anything that declines to
    judge old stamps also fails to reclaim genuinely crashed sends.

    Fails on base: stamp is written as real-now, not T0−901, so age ≈ 0 s and
    the reclaimer leaves the row alone.
    """
    ob = pg_outbox
    T0 = datetime.now(timezone.utc).replace(microsecond=0)
    T_WRITE = T0 - timedelta(seconds=MAX_AGE + 1)

    _pin_db_clock(T_WRITE)
    msg = ob.add("t1", SITE, "item_stale", sequence="presale", step_id="s",
                 step_label="S", body="b", auto=False)
    ob.set_status(msg["id"], ob.SENDING)

    _pin_db_clock(T0)
    requeued = ob.reclaim_stuck_sending(max_age_seconds=MAX_AGE)

    assert requeued == 1, (
        "a send whose DB-clock age exceeds MAX_AGE must be requeued — "
        "if this fails on base, that is expected (base uses real clock)"
    )
    assert ob.get(msg["id"])["status"] == ob.QUEUED


# ---------------------------------------------------------------------------
# Positive guards — pass on both base and fix
# ---------------------------------------------------------------------------

def test_live_send_not_requeued_one_second_old(pg_outbox):
    """A send claimed one second ago (DB clock) must never be requeued."""
    ob = pg_outbox
    T0 = datetime.now(timezone.utc).replace(microsecond=0)

    _pin_db_clock(T0)
    msg = ob.add("t1", SITE, "item_live", sequence="presale", step_id="s",
                 step_label="S", body="b", auto=False)
    ob.set_status(msg["id"], ob.SENDING)

    _pin_db_clock(T0 + timedelta(seconds=1))
    requeued = ob.reclaim_stuck_sending(max_age_seconds=MAX_AGE)

    assert requeued == 0, "a 1-second-old send must not be requeued"
    assert ob.get(msg["id"])["status"] == ob.SENDING


def test_send_at_exact_age_boundary_not_requeued(pg_outbox):
    """A send whose age equals exactly MAX_AGE seconds is at the boundary, not past it."""
    ob = pg_outbox
    T0 = datetime.now(timezone.utc).replace(microsecond=0)
    T_WRITE = T0 - timedelta(seconds=MAX_AGE)

    _pin_db_clock(T_WRITE)
    msg = ob.add("t1", SITE, "item_boundary", sequence="presale", step_id="s",
                 step_label="S", body="b", auto=False)
    ob.set_status(msg["id"], ob.SENDING)

    _pin_db_clock(T0)
    requeued = ob.reclaim_stuck_sending(max_age_seconds=MAX_AGE)

    assert requeued == 0, f"age == MAX_AGE ({MAX_AGE} s) must not trigger a requeue"
    assert ob.get(msg["id"])["status"] == ob.SENDING


def test_send_one_second_past_boundary_is_requeued(pg_outbox):
    """A send one second past MAX_AGE must be requeued."""
    ob = pg_outbox
    T0 = datetime.now(timezone.utc).replace(microsecond=0)
    T_WRITE = T0 - timedelta(seconds=MAX_AGE + 1)

    _pin_db_clock(T_WRITE)
    msg = ob.add("t1", SITE, "item_over", sequence="presale", step_id="s",
                 step_label="S", body="b", auto=False)
    ob.set_status(msg["id"], ob.SENDING)

    _pin_db_clock(T0)
    requeued = ob.reclaim_stuck_sending(max_age_seconds=MAX_AGE)

    assert requeued == 1
    assert ob.get(msg["id"])["status"] == ob.QUEUED
