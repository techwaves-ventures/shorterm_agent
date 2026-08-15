"""VEN-142: `jobs.py` compares timestamps across the web/worker host boundary.

`jobs.py` exists so a Vercel web dyno and a Playwright worker VM can talk through
one shared `DATABASE_URL`. Three of its columns are written on one of those hosts
and read on the other, and all three were naive server-local wall clock:

  * `ff_worker.last_seen`  — written by `heartbeat` (worker), read by
    `worker_online` (web). `WORKER_TTL_SECONDS = 90`, so *any* nonzero offset
    breaks it. Westward the healthy worker reads permanently offline and
    `reap_stale` kills live scrapes on every dashboard poll; eastward it reads
    permanently online and crash recovery never fires.
  * `ff_jobs.created_at`   — written by the web host at `enqueue`, read by the
    worker at startup (`worker.py` -> `reap_stale(active_worker_id=...)`).
    Westward the `MAX_ACTIVE_JOB_SECONDS` backstop silently never trips.
  * `ff_jobs.updated_at`   — written by the worker (`set_status`), read by the web
    host (`_cooldown_remaining`). Westward the FurnishedFinder login-email burst
    guard is defeated; eastward it reports an absurd cooldown that locks the
    tenant out of retrying.

Why a *matrix* over host offsets rather than one stale-stamp case: two of the
three harms are one-directional, and a single-direction test passes a
one-directional bug. Each test carries a same-offset (`WEB`) case so the offset
dimension is isolated rather than assumed.

The `hosts` fixture asserts the process really changed zone. `TZ` degrades
silently to UTC when the zoneinfo is missing, and a cross-host test that never
actually moved scores a perfect pass against unfixed code.

Verified against `6f62a57`, the parent of this change: **20 of these 32 cases fail
there and all 32 pass here.** Two distinct failure modes, and it is worth being
precise about which is which, because only the first is the filed defect:

  * *Offset* failures — the filed harm, and one-directional exactly as filed.
    `test_live_running_job_survives_an_ordinary_web_poll` fails on `6f62a57` for
    `America/Los_Angeles` only (`assert 'error' == 'running'`: a live job killed
    mid-run), and passes for `UTC` and every eastward zone. Likewise the healthy
    worker reads offline only westward, and the cooldown is defeated westward /
    absurd eastward.
  * *Shape* failures — `6f62a57` cannot read an absolute stamp at all. Where a
    test writes the offset-carrying stamp this change introduces, the old reader
    raises `TypeError: can't subtract offset-naive and offset-aware datetimes`
    (`worker_online`) or swallows it into `None` via a bare `except`
    (`_age_seconds`). That is why the `UTC` cases of the dead-worker and wedged-job
    tests also fail on base — *not* because a same-offset deploy was ever broken.
    It is the mixed-rollout hazard: the reader must ship before or with the
    writer, never after.

The pre-existing suite is green on *both* heads (314 passed, 5 xfailed), which is
exactly why these were needed.
"""
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("SQLITE_PATH", "/tmp/ven142-unused.db")
os.environ.setdefault("FF_CRED_KEY", "c9jwUi0L-fUjf3wjbq74M0lK3ah7fmEfGhjxZ7RehQk=")
os.environ.setdefault("SECRET_KEY", "test-secret")

import db  # noqa: E402
import jobs  # noqa: E402

WEB = "UTC"
# Worker zones spanning both directions, including the half/quarter-hour offsets
# that a naive comparison also gets wrong.
WEST = "America/Los_Angeles"    # UTC-7/-8
EAST = "Pacific/Auckland"       # UTC+12/+13
HALF = "Asia/Kolkata"           # UTC+5:30
QUARTER = "Asia/Kathmandu"       # UTC+5:45
CHATHAM = "Pacific/Chatham"      # UTC+12:45

OFFSETS = [WEB, WEST, EAST, HALF, QUARTER, CHATHAM]


@pytest.fixture()
def hosts(tmp_path, monkeypatch):
    """One shared DB plus a `host(tz)` switch that proves the zone really moved.

    Restores the original TZ on teardown so a moved clock cannot leak into the
    rest of the suite.
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "ven142.db")
    original = os.environ.get("TZ")

    def host(tz):
        os.environ["TZ"] = tz
        time.tzset()
        offset = datetime.now().astimezone().utcoffset()
        assert offset is not None, f"no usable zoneinfo for {tz}"
        return offset

    # A cross-host test is only meaningful if these are genuinely different.
    assert host(WEB) != host(WEST) != host(EAST), "TZ switching is not working"
    yield host

    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


def _reset():
    with jobs._conn() as c:
        c.execute("DELETE FROM ff_jobs")
        c.execute("DELETE FROM ff_worker")


# --- ff_worker.last_seen ---------------------------------------------------


@pytest.mark.parametrize("worker_tz", OFFSETS)
def test_healthy_worker_reads_online_from_any_offset(hosts, worker_tz):
    """A worker that heartbeated *just now* must read online from the web host."""
    _reset()
    hosts(worker_tz)
    jobs.heartbeat("w1")
    hosts(WEB)
    assert jobs.worker_online() is True, (
        f"worker in {worker_tz} heartbeated 0s ago but reads offline from {WEB}"
    )


@pytest.mark.parametrize("worker_tz", OFFSETS)
def test_dead_worker_reads_offline_from_any_offset(hosts, worker_tz):
    """Crash recovery depends on a stopped worker actually reading offline."""
    _reset()
    hosts(worker_tz)
    jobs.heartbeat("w2")
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    with jobs._conn() as c:
        c.execute("UPDATE ff_worker SET last_seen=? WHERE id=1", (stale,))
    hosts(WEB)
    assert jobs.worker_online() is False, (
        f"worker in {worker_tz} last beat 1h ago but still reads online from {WEB}"
    )


@pytest.mark.parametrize("worker_tz", OFFSETS)
def test_live_running_job_survives_an_ordinary_web_poll(hosts, worker_tz):
    """The headline harm: `reap_stale` killing a healthy scrape mid-run.

    Every dashboard `/api/status` poll calls `reap_stale`. With the worker read as
    offline, a `running` job owned by a live worker is failed with
    STALE_JOB_MESSAGE and the FF account is flipped to `error`.
    """
    _reset()
    tenant = f"t-live-{worker_tz.replace('/', '-')}"
    hosts(worker_tz)
    jobs.enqueue(tenant)
    jobs.heartbeat("w3")
    job = jobs.claim_next("w3")
    assert job is not None, "precondition: the worker must have claimed the job"
    assert job["status"] == jobs.RUNNING
    jobs.heartbeat("w3")           # still alive, mid-scrape
    hosts(WEB)

    jobs.reap_stale()              # an ordinary dashboard poll

    assert (jobs.latest(tenant) or {})["status"] == jobs.RUNNING, (
        f"live job owned by a heartbeating {worker_tz} worker was reaped by a {WEB} poll"
    )


# --- ff_jobs.created_at ----------------------------------------------------


@pytest.mark.parametrize("reader_tz", OFFSETS)
def test_wedged_job_is_reaped_from_any_reader_offset(hosts, reader_tz):
    """`MAX_ACTIVE_JOB_SECONDS` is the only backstop for a wedged-but-alive worker.

    `created_at` is written by the web host; the worker reads it at startup. If
    the reader's offset makes the age look small (or negative), the backstop never
    trips and the tenant is stuck on "Checking…" with no route out.
    """
    _reset()
    tenant = f"t-wedge-{reader_tz.replace('/', '-')}"
    hosts(WEB)
    jobs.enqueue(tenant)           # created_at written by WEB
    jobs.heartbeat("w4")
    job = jobs.claim_next("w4")
    assert job is not None
    wedged = (datetime.now(timezone.utc)
              - timedelta(seconds=jobs.MAX_ACTIVE_JOB_SECONDS + 600))
    with jobs._conn() as c:
        c.execute("UPDATE ff_jobs SET created_at=? WHERE id=?",
                  (wedged.isoformat(timespec="seconds"), job["id"]))

    hosts(reader_tz)
    jobs.heartbeat("w4")           # worker ALIVE -> only the cap can free this job
    reaped = jobs.reap_stale(active_worker_id="w4")

    assert reaped >= 1, (
        f"a job wedged {jobs.MAX_ACTIVE_JOB_SECONDS + 600}s was not reaped by a "
        f"{reader_tz} reader; the wedged-job backstop is disabled"
    )


def test_legacy_naive_created_at_same_host_is_still_reaped(hosts):
    """Regression pin (plan AC8): do NOT "decline to judge" a naive stamp here.

    A row written by the previous release carries a naive `created_at`. Refusing
    to age it (returning None, as `outbox.reclaim_stuck_sending` does for its own
    column) would skip the backstop — and for `created_at` that age is the last
    line of defence, so the tenant would be stranded on "Checking…" forever with
    no route out. That is worse than the bug being fixed, it fires on the ordinary
    single-host UTC deploy, and the suite cannot see it.

    A naive stamp is therefore read as the reader's own wall clock, which is what
    the previous release did — so this case behaves no worse than `6f62a57`.
    """
    _reset()
    tenant = "t-legacy-ac8"
    hosts(WEB)
    jobs.enqueue(tenant)
    jobs.heartbeat("w5")
    job = jobs.claim_next("w5")
    assert job is not None
    legacy = (datetime.now()          # naive, as the previous release wrote it
              - timedelta(seconds=jobs.MAX_ACTIVE_JOB_SECONDS + 600))
    with jobs._conn() as c:
        c.execute("UPDATE ff_jobs SET created_at=? WHERE id=?",
                  (legacy.isoformat(timespec="seconds"), job["id"]))

    jobs.heartbeat("w5")              # same host, same zone, worker alive
    assert jobs.reap_stale(active_worker_id="w5") >= 1, (
        "a legacy naive created_at must still be aged, or a wedged job strands forever"
    )


# --- ff_jobs.updated_at ----------------------------------------------------


@pytest.mark.parametrize("worker_tz", OFFSETS)
def test_login_email_cooldown_is_sane_from_any_writer_offset(hosts, worker_tz):
    """The cooldown stops repeated Check-now clicks bursting real FF login emails.

    `updated_at` is stamped by the worker when the login fails and read by the web
    host. Westward the cooldown reads already-expired (burst guard defeated);
    eastward it reads as a nonsense multi-hour wait the tenant cannot clear.
    """
    _reset()
    tenant = f"t-cool-{worker_tz.replace('/', '-')}"
    hosts(WEB)
    job = jobs.enqueue(tenant)

    hosts(worker_tz)                 # the worker fails the login and stamps it
    jobs.set_status(job["id"], jobs.ERROR,
                    "Couldn't verify your FurnishedFinder login.")

    hosts(WEB)                       # tenant immediately clicks Check now
    again = jobs.enqueue(tenant)
    assert again["id"] == job["id"], (
        f"a {worker_tz} worker's failed login did not hold the retry from {WEB}: "
        "the FF login-email burst guard is defeated"
    )

    remaining = jobs._cooldown_remaining(jobs.latest(tenant))
    assert 0 < remaining <= jobs.ERROR_RETRY_COOLDOWN_SECONDS, (
        f"cooldown from a {worker_tz} writer is {remaining}s, outside "
        f"(0, {jobs.ERROR_RETRY_COOLDOWN_SECONDS}] — the tenant sees a nonsense wait"
    )


# --- the mechanism, not just the symptom -----------------------------------


def test_every_cross_host_column_is_written_absolute(hosts):
    """Assert the *mechanism*, so a later refactor back to a naive writer is caught.

    The behavioural tests above are all satisfiable by a reader-side hack; this
    one pins the actual contract: every stamp this module writes carries an
    offset. `_age_seconds` deliberately still accepts naive input (legacy rows),
    so nothing else would notice a regressed writer.
    """
    _reset()
    hosts(WEST)
    jobs.heartbeat("w6")
    job = jobs.enqueue("t-mech")
    jobs.set_status(job["id"], jobs.ERROR, "nope")

    with jobs._conn() as c:
        last_seen = c.execute("SELECT last_seen FROM ff_worker WHERE id=1").fetchone()[0]
        created, updated = c.execute(
            "SELECT created_at, updated_at FROM ff_jobs WHERE id=?", (job["id"],)
        ).fetchone()

    for column, value in (("ff_worker.last_seen", last_seen),
                          ("ff_jobs.created_at", created),
                          ("ff_jobs.updated_at", updated)):
        assert datetime.fromisoformat(str(value)).tzinfo is not None, (
            f"{column} was written naive ({value!r}); it is read on another host"
        )
