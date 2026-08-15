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

Most of these cases fail against `6f62a57`, the parent of this change, and all pass
here. But the failures come in two kinds, and only the first is the filed defect —
counting them together would overstate what the offset bug costs:

  * *Offset* failures — the filed harm, one-directional exactly as filed. Seven
    cases: `test_live_running_job_survives_an_ordinary_web_poll` fails for
    `America/Los_Angeles` only (`assert 'error' == 'running'`: a live job killed
    mid-run) and passes at `UTC` and eastward; the healthy worker reads offline
    only westward; the cooldown is defeated westward and absurd eastward
    (`Auckland`, `Kolkata`, `Kathmandu`, `Chatham`).
  * *Shape* failures — `6f62a57` cannot read an absolute stamp at all, so any case
    that writes the offset-carrying stamp this change introduces fails there
    regardless of offset. `worker_online` raises `TypeError: can't subtract
    offset-naive and offset-aware datetimes`; `_age_seconds` swallows it to `None`
    via a bare `except` and the cap is skipped. This accounts for *every* zone of
    `test_dead_worker_reads_offline_from_any_offset` and
    `test_wedged_job_is_reaped_from_any_reader_offset`, not merely their `UTC`
    cases, and it says nothing about whether a same-offset deploy was broken.
    It is the mixed-rollout hazard, and it is the reason the reader must ship
    before or with the writer, never after (see DEPLOY.md).

Two consequences of that split are worth stating plainly rather than leaving to be
rediscovered. First, the eastward `last_seen` harm ("crash recovery never fires")
and the westward `created_at` harm ("the cap silently never trips") are **not**
cured by this change alone for stamps a not-yet-upgraded host wrote — only once the
*writer* is upgraded. Second, `created_at` is written once at `enqueue` and never
re-stamped, so unlike `last_seen` it does not converge on its own.

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
    # Compared pairwise on purpose: `a != b != c` is `a != b and b != c` and never
    # compares a to c, so it would pass with WEB and EAST identical.
    _offsets = {z: host(z) for z in (WEB, WEST, EAST)}
    assert len(set(_offsets.values())) == 3, f"TZ switching is not working: {_offsets}"
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


@pytest.mark.parametrize("single_host_tz", [WEST, EAST, HALF])
def test_legacy_naive_stamps_are_unchanged_on_a_single_host_deploy(hosts, single_host_tz):
    """The "no worse than base" contract, on the topology that actually has legacy rows.

    A naive stamp must be read as the *reader's* wall clock, not as UTC. On the
    ordinary single-host deploy — one box, one zone, writer and reader identical —
    naive stamps are already correct, and reading them as UTC would shift every
    legacy row by that host's offset. For a box in Los Angeles that turns a
    60-second-old failed login into a 7-hour-old one, silently switching off the
    FurnishedFinder login-email burst guard on the *ordinary* deploy as the price
    of fixing the split one.

    Parameterized over a non-UTC host on purpose: at UTC the two readings
    coincide, so a UTC-only test cannot see this and a mutation to
    `replace(tzinfo=utc)` survives it.
    """
    _reset()
    tenant = f"t-single-{single_host_tz.replace('/', '-')}"
    hosts(single_host_tz)             # ONE host: it both writes and reads
    job = jobs.enqueue(tenant)
    jobs.set_status(job["id"], jobs.ERROR, "Couldn't verify your FF login.")

    # Rewrite as the PREVIOUS release did: naive local wall clock, 60s ago.
    legacy = (datetime.now() - timedelta(seconds=60)).isoformat(timespec="seconds")
    with jobs._conn() as c:
        c.execute("UPDATE ff_jobs SET updated_at=? WHERE id=?", (legacy, job["id"]))

    remaining = jobs._cooldown_remaining(jobs.latest(tenant))
    expected = jobs.ERROR_RETRY_COOLDOWN_SECONDS - 60
    assert abs(remaining - expected) <= 2, (
        f"a legacy naive stamp 60s old on a single {single_host_tz} host reads as "
        f"{remaining}s remaining, expected ~{expected}s: naive stamps are no longer "
        "read as the reader's own wall clock, so the burst guard breaks on the "
        "ordinary single-host deploy"
    )


@pytest.mark.parametrize(
    "label,frozen_utc,naive_stamp,true_age",
    [
        # LA fall-back 2026: 02:00 PDT -> 01:00 PST. "now" is 01:31 PST (09:31 UTC);
        # the stamp reads 01:30, one minute earlier in the PST repetition.
        ("fall-back repeated hour", datetime(2026, 11, 1, 9, 31, tzinfo=timezone.utc),
         "2026-11-01T01:30:00", 60),
        # LA spring-forward 2026: 02:00 PST -> 03:00 PDT. "now" is 03:01 PDT (10:01 UTC).
        ("spring-forward gap", datetime(2026, 3, 8, 10, 1, tzinfo=timezone.utc),
         "2026-03-08T03:00:00", 60),
        # A stamp a day before the transition, read after it. `now` is Nov 2
        # 01:00 PST; the stamp reads Nov 1 00:00, so the wall clock advanced 25h
        # even though 26h of real time elapsed. The naive reading is the 25h one,
        # which is what the previous release computed and therefore the contract.
        ("across the transition", datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc),
         "2026-11-01T00:00:00", 25 * 3600),
    ],
)
def test_naive_stamp_ages_the_same_across_a_dst_boundary(
    hosts, monkeypatch, label, frozen_utc, naive_stamp, true_age
):
    """A naive stamp must not gain or lose an hour when DST moves under it.

    `stamp.astimezone()` looks correct and is not: it resolves the offset as of the
    *stamp's* wall clock (`fold=0`), while "now" carries the offset in force now.
    They disagree by the DST delta whenever a transition falls between the two, so
    inside the fall-back hour a 60-second-old heartbeat aged as 3660 seconds —
    failing a live worker as offline, reaping the `running` job it was driving, and
    zeroing the cooldown. That is every harm this module's fix exists to remove,
    re-created on a single-host deploy the previous release handled correctly.

    The previous release subtracted wall clock from wall clock and was immune, so
    the correct reading of a naive stamp is the naive one. Pinned here because the
    DST hour is unreachable from any test that stamps relative to a real "now".
    """
    hosts(WEST)                       # a DST-observing zone
    _reset()

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return (frozen_utc.astimezone(tz) if tz
                    else frozen_utc.astimezone().replace(tzinfo=None))

    monkeypatch.setattr(jobs, "datetime", Frozen)

    age = jobs._age_seconds(naive_stamp)
    assert age == pytest.approx(true_age, abs=1), (
        f"{label}: a naive stamp {true_age}s old aged as {age}s — off by "
        f"{(age or 0) - true_age}s, i.e. the DST delta"
    )

    # And the consequence, through the real reader.
    with jobs._conn() as c:
        c.execute("INSERT INTO ff_worker (id, worker_id, last_seen) VALUES (1,?,?)",
                  ("w-dst", naive_stamp))
    expected_online = true_age <= jobs.WORKER_TTL_SECONDS
    assert jobs.worker_online() is expected_online, (
        f"{label}: worker {true_age}s stale read online={not expected_online}"
    )


def test_future_dated_stamp_still_reads_online_as_before(hosts):
    """A clock-skewed future stamp must behave exactly as it did on `6f62a57`.

    Base computed `datetime.now() - last <= TTL`, which is True for a negative
    age, so a future-dated `last_seen` read *online*. This change keeps that
    (`age <= TTL`) rather than quietly clamping with `abs()`, which would flip a
    skewed worker to offline and start reaping its live jobs.

    Future-dated stamps from host clock skew are a separate filed defect
    (VEN-133); this test only pins that VEN-142 does not change the behaviour
    while passing through.

    Both shapes are checked. The naive one is the case that actually pins parity
    with `6f62a57` — an *aware* future stamp makes the old reader raise `TypeError`,
    so on base it fails on shape and proves nothing about the skew behaviour.
    """
    for label, ahead in (
        ("naive", (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")),
        ("aware", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")),
    ):
        _reset()
        hosts(WEB)
        jobs.heartbeat("w7")
        with jobs._conn() as c:
            c.execute("UPDATE ff_worker SET last_seen=? WHERE id=1", (ahead,))

        assert jobs.worker_online() is True, (
            f"a future-dated ({label}) last_seen changed meaning; base read it as "
            "online and reaping a skewed worker's live jobs is a regression, not a fix"
        )


def test_the_two_thresholds_are_exact(hosts, monkeypatch):
    """Pin `<=` on the TTL and `>` on the cap; both boundaries survived mutation.

    `worker_online` uses `age <= WORKER_TTL_SECONDS` and `reap_stale` uses
    `age > MAX_ACTIVE_JOB_SECONDS`. Flipping either to its neighbour left every
    other case green, so the exact comparison was untested.

    The clock is frozen rather than measured: with a real `now`, the microseconds
    between writing the stamp and reading it push an exactly-at-the-threshold age
    just over the line, so the boundary can only be hit deterministically by
    pinning `now`.
    """
    hosts(WEB)
    frozen = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return (frozen.astimezone(tz) if tz
                    else frozen.astimezone().replace(tzinfo=None))

    monkeypatch.setattr(jobs, "datetime", Frozen)

    def stamp_at(seconds_ago):
        return (frozen - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")

    # last_seen exactly at the TTL is still online; one second past it is not.
    for offset, expected in ((jobs.WORKER_TTL_SECONDS, True),
                             (jobs.WORKER_TTL_SECONDS + 1, False)):
        _reset()
        with jobs._conn() as c:
            c.execute("INSERT INTO ff_worker (id, worker_id, last_seen) VALUES (1,?,?)",
                      ("w8", stamp_at(offset)))
        assert jobs.worker_online() is expected, (
            f"last_seen exactly {offset}s old: expected online={expected} "
            f"(TTL={jobs.WORKER_TTL_SECONDS}, boundary is inclusive)"
        )

    # created_at exactly at the cap is NOT reaped; one second past it is.
    for offset, expected in ((jobs.MAX_ACTIVE_JOB_SECONDS, 0),
                             (jobs.MAX_ACTIVE_JOB_SECONDS + 1, 1)):
        _reset()
        jobs.enqueue(f"t-bound-{offset}")
        jobs.heartbeat("w8")                     # worker alive: only the cap applies
        job = jobs.claim_next("w8")
        with jobs._conn() as c:
            c.execute("UPDATE ff_jobs SET created_at=? WHERE id=?",
                      (stamp_at(offset), job["id"]))
        assert jobs.reap_stale(active_worker_id="w8") == expected, (
            f"created_at exactly {offset}s old: expected reaped={expected} "
            f"(cap={jobs.MAX_ACTIVE_JOB_SECONDS}, boundary is exclusive)"
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
