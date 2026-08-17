"""VEN-137: `worker_online()` must agree across a split-timezone deploy.

Standalone (no pytest dependency required to run it directly):

    PYTHONPATH=. ./.venv/bin/python tests/test_worker_online_tz.py

`ff_worker.last_seen` is written by the *worker* host (`jobs.heartbeat`) and
read by the *web* host (`jobs.worker_online`). The documented production shape
runs those as separate processes over one shared database (`Procfile`,
`DEPLOY.md`, `docker-compose.yml`) and nothing pins `TZ` on either. While the
stamp was naive local wall-clock, the comparison was off by the offset between
the two hosts -- which always exceeds the 90s TTL, so the beacon did not
degrade, it inverted:

  * a worker to the *west* read permanently OFFLINE while perfectly healthy, so
    `reap_stale()` destroyed its live in-flight jobs;
  * a worker to the *east* stamped the future, making the subtraction negative,
    so it read ONLINE *forever -- including after it had crashed*, which
    defeats the liveness beacon at the exact moment it is load-bearing.

Why this test spawns real subprocesses: `TZ` only takes effect at process start
(via `tzset`), and the whole defect lives in the *pairing* of two clocks. A
single-process test is structurally incapable of reaching it -- note that every
cell on the writer==reader diagonal is correct even on unfixed code, which is
why the rest of the suite never caught this.
"""
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO)

import jobs  # noqa: E402

# A west/east spread either side of UTC, plus a half-hour zone so the test does
# not silently assume whole-hour offsets.
ZONES = ["UTC", "America/Los_Angeles", "Asia/Tokyo", "Pacific/Auckland",
         "Asia/Kolkata"]

_FAILURES = []


def check(cond, msg):
    """Print like the rest of the suite, but actually fail.

    Deliberately *not* the record-only `check()` this suite used to carry, which
    appended to a module-level list. That variant reported correctly when the
    file was run directly, but under `pytest` the collected `test_*` function
    returned normally and the failure was swallowed -- a test that cannot fail.
    This one raises, so it works both ways. `test_ff_connect_flow.check()` was
    the last record-only holdout and now raises as well (VEN-166); keep any new
    `check()` helper raising.
    """
    if cond:
        print("  ok  %s" % msg)
        return
    print("  FAIL %s" % msg)
    _FAILURES.append(msg)
    raise AssertionError(msg)


# The writer heartbeats with its own clock shifted back by AGE seconds, so the
# stamp it writes is byte-identical to what a genuinely AGE-second-old heartbeat
# would have written. We move the clock, not the data.
_WRITER = r'''
import os, sys
from datetime import datetime, timedelta
AGE = float(os.environ["AGE"])
sys.path.insert(0, os.environ["REPO"])
import jobs

class Shifted(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) - timedelta(seconds=AGE)

print("OFFSET:%s" % datetime.now().astimezone().utcoffset().total_seconds())
jobs.datetime = Shifted
jobs.heartbeat("w1")
print("RESULT:OK")
'''

_READER = r'''
import os, sys
from datetime import datetime
sys.path.insert(0, os.environ["REPO"])
import jobs
print("OFFSET:%s" % datetime.now().astimezone().utcoffset().total_seconds())
try:
    print("RESULT:%s" % jobs.worker_online())
except Exception as e:
    print("RESULT:CRASH:%s: %s" % (type(e).__name__, e))
'''

# Seeds a *legacy naive* row, as a pre-fix worker would have left behind.
_LEGACY = r'''
import os, sys
from datetime import datetime, timedelta
sys.path.insert(0, os.environ["REPO"])
import jobs
jobs.heartbeat("w1")
naive = (datetime.now() - timedelta(seconds=float(os.environ["AGE"]))).isoformat(timespec="seconds")
with jobs._conn() as c:
    c.execute("UPDATE ff_worker SET last_seen=? WHERE id=1", (naive,))
print("OFFSET:%s" % datetime.now().astimezone().utcoffset().total_seconds())
print("RESULT:OK")
'''


# Reads the stored column back verbatim. Must run in a child: `db.DB_PATH` is
# bound when `db` is first imported, so pointing `SQLITE_PATH` at a temp file
# from the parent after import has no effect (reloading `jobs` does not rebind
# it either) -- the write would silently land in the suite's own database.
_READ_RAW = r'''
import os, sys
sys.path.insert(0, os.environ["REPO"])
import jobs
with jobs._conn() as c:
    row = c.execute("SELECT last_seen FROM ff_worker WHERE id=1").fetchone()
print("RESULT:%s" % (row[0] if row else None))
'''


# Reports whether the child is sitting just past a DST fall-back, so the caller
# can prove the ambiguous-wall-clock precondition actually held rather than
# assume it. `time.localtime` needs no tz database for a POSIX TZ string.
_FOLD_PROBE = r'''
import time
now = time.time()
print("ISDST_NOW:%d" % time.localtime(now).tm_isdst)
print("ISDST_BEFORE:%d" % time.localtime(now - 2700).tm_isdst)
print("RESULT:OK")
'''


def _fold_tz(when=None):
    """A POSIX TZ string whose DST fall-back happened ~30 minutes ago.

    Built from the current instant rather than hardcoded, because a real zone
    folds on one night a year and a fixed rule would make this test dead code
    for the other 364. Standard offset is UTC+0 and DST is UTC+1, so the
    transition rewinds the wall clock by an hour and every local time in the
    surrounding hour occurs twice -- `now` among them.

    The DST *start* rule is placed one month after the end rule (POSIX allows
    the wrapped, southern-hemisphere ordering), which keeps "we are in DST
    right up until the end rule" true whatever month the test runs in.
    """
    u = when or datetime.now(timezone.utc)
    # The transition fires at u-30min UTC; named on the DST clock (UTC+1) that
    # is u+30min, which is what the POSIX rule's date and time must express.
    d = u + timedelta(minutes=30)
    end = "M%d.%d.%d/%s" % (d.month, ((d.day - 1) // 7) + 1,
                            (d.weekday() + 1) % 7, d.strftime("%H:%M:%S"))
    start = "M%d.1.0/00:00:00" % ((d.month % 12) + 1)
    return "XXX0YYY,%s,%s" % (start, end)


def _expected_offset(zone):
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(zone)).utcoffset().total_seconds()


def _run(src, zone, db, age=0):
    env = dict(os.environ)
    env.update({"TZ": zone, "REPO": REPO, "SQLITE_PATH": db, "AGE": str(age)})
    # An inherited TZDIR pointing somewhere without a tz database would make
    # every zone resolve to UTC -- see _assert_real_zone.
    env.pop("TZDIR", None)
    p = subprocess.run([sys.executable, "-c", src], env=env,
                       capture_output=True, text=True, timeout=120)
    off = res = None
    for line in p.stdout.splitlines():
        if line.startswith("OFFSET:"):
            off = float(line.split(":", 1)[1])
        elif line.startswith("RESULT:"):
            res = line.split(":", 1)[1]
    if res is None:
        raise AssertionError("child under TZ=%s produced no result (rc=%s): %s"
                             % (zone, p.returncode, p.stderr.strip()[-400:]))
    return off, res


def _probe_lines(src, zone):
    """Raw stdout lines from a child under `zone`, for probes whose output is
    not the OFFSET/RESULT pair `_run` expects."""
    env = dict(os.environ)
    env.update({"TZ": zone, "REPO": REPO})
    env.pop("TZDIR", None)
    p = subprocess.run([sys.executable, "-c", src], env=env,
                       capture_output=True, text=True, timeout=120)
    if "RESULT:OK" not in p.stdout:
        raise AssertionError("probe under TZ=%s failed (rc=%s): %s"
                             % (zone, p.returncode, p.stderr.strip()[-400:]))
    return p.stdout.splitlines()


def _assert_real_zone(zone, actual):
    """Guard against a vacuous run.

    `TZ` degrades to UTC *silently, with no exception* when the zone cannot be
    resolved (an unset-`TZDIR` / missing-`tzdata` image -- the deploy base is
    `python:3.12-slim`, where `tzdata` is not guaranteed). Every cell would then
    collapse onto the always-correct diagonal, this test would pass on unfixed
    code, and it would be silently worthless. So verify each child really got
    the zone it was told to use before believing anything it reports.
    """
    want = _expected_offset(zone)
    if actual is None or abs(actual - want) > 1:
        raise AssertionError(
            "TZ=%s resolved to offset %s, expected %s -- no tz database, so this "
            "test would be vacuous. Install tzdata rather than skipping."
            % (zone, actual, want))


@contextlib.contextmanager
def _scratch_db():
    """A throwaway DB path, cleaned up afterwards.

    Each cell needs a virgin database, and there are len(ZONES)**2 of them per
    matrix -- left behind, they accumulate in $TMPDIR run after run.
    """
    d = tempfile.mkdtemp(prefix="ven137_")
    try:
        yield os.path.join(d, "t.db")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _matrix(age):
    """worker_online() for every (writer TZ, reader TZ) pair at a given age."""
    grid = {}
    for zw in ZONES:
        for zr in ZONES:
            with _scratch_db() as db:
                offw, _ = _run(_WRITER, zw, db, age)
                _assert_real_zone(zw, offw)
                offr, res = _run(_READER, zr, db, age)
                _assert_real_zone(zr, offr)
                grid[(zw, zr)] = res
    return grid


def test_live_worker_reads_online_in_every_timezone_pair():
    """A genuinely recent heartbeat must read ONLINE in all 16 pairs.

    Unfixed, the westward pairs read OFFLINE, and `reap_stale()` then fails the
    live in-flight jobs of a perfectly healthy worker.
    """
    grid = _matrix(age=0)
    wrong = {k: v for k, v in grid.items() if v != "True"}
    check(not wrong, "live worker reads ONLINE in all %d TZ pairs (wrong: %s)"
          % (len(grid), sorted("%s->%s=%s" % (w, r, v) for (w, r), v in wrong.items())))


def test_dead_worker_reads_offline_in_every_timezone_pair():
    """A heartbeat older than the TTL must read OFFLINE in all 16 pairs."""
    grid = _matrix(age=10 * jobs.WORKER_TTL_SECONDS)
    wrong = {k: v for k, v in grid.items() if v != "False"}
    check(not wrong, "dead worker reads OFFLINE in all %d TZ pairs (wrong: %s)"
          % (len(grid), sorted("%s->%s=%s" % (w, r, v) for (w, r), v in wrong.items())))


def test_eastward_dead_worker_is_not_reported_online():
    """The specific case a single-process test cannot reach, asserted by name.

    An eastward worker stamps the future, so `now - last` is negative and
    satisfies `<= TTL` unconditionally: the crashed worker reads ONLINE
    forever. This is the dangerous half of the defect -- the operator is told
    automation is running when it is not, and stranded jobs are never reaped.
    """
    with _scratch_db() as db:
        offw, _ = _run(_WRITER, "Pacific/Auckland", db, age=10 * jobs.WORKER_TTL_SECONDS)
        _assert_real_zone("Pacific/Auckland", offw)
        offr, res = _run(_READER, "UTC", db)
        _assert_real_zone("UTC", offr)
    check(res == "False",
          "worker dead %ds in Pacific/Auckland reads OFFLINE to a UTC web host, "
          "not falsely ONLINE (got %s)" % (10 * jobs.WORKER_TTL_SECONDS, res))


def test_legacy_naive_row_does_not_crash_the_reader():
    """A pre-fix worker's naive row must not 500 the dashboard.

    `worker_online()` is reached from `public_state()`, the dashboard's
    `/api/status` poll, and subtracting naive from aware raises `TypeError` --
    which the `except ValueError` there does *not* catch. Without the tzinfo
    branch this is an uncaught 500 for every user until the first new heartbeat.
    """
    for age, expected in ((0, "True"), (10 * jobs.WORKER_TTL_SECONDS, "False")):
        with _scratch_db() as db:
            _run(_LEGACY, "UTC", db, age)
            _, res = _run(_READER, "UTC", db)
        # Same-zone, so the pre-fix comparison the fallback reuses is correct
        # here -- this pins that the legacy branch still answers, and answers
        # exactly what unfixed code answered, rather than merely not raising.
        check(res == expected,
              "legacy naive row (age=%ds) returns %s as before, no TypeError (got %s)"
              % (age, expected, res))


def test_legacy_naive_row_survives_an_ambiguous_wall_clock():
    """The legacy fallback must not newly break a *single-host* deploy.

    `test_legacy_naive_row_does_not_crash_the_reader` above pins the same
    promise but runs both children under `TZ=UTC`, where no wall clock is ever
    ambiguous -- so it cannot see the one case where "read the naive stamp as
    the reader's own local time" and "subtract the wall clocks raw" disagree.

    That case is the hour after a DST fall-back, when the same naive wall time
    occurred twice. Resolving it via `astimezone()` silently picks the earlier
    (DST) occurrence, which reads a heartbeat from seconds ago as a full hour
    old -- so a live worker reports OFFLINE and `reap_stale()` destroys its
    in-flight jobs, on a single-host deploy that has no timezone split at all.
    This fix's fallback avoids that by reusing the pre-fix subtraction verbatim,
    and this pins that it stays that way: the promise in `_now_utc` is that the
    legacy branch is never *newly* wrong, and an untested promise is a wish.
    """
    tz = _fold_tz()
    isdst_now = isdst_before = None
    for line in _probe_lines(_FOLD_PROBE, tz):
        if line.startswith("ISDST_NOW:"):
            isdst_now = int(line.split(":", 1)[1])
        elif line.startswith("ISDST_BEFORE:"):
            isdst_before = int(line.split(":", 1)[1])
    # Guard the precondition. If the constructed rule did not actually put the
    # child just past a fall-back, the assertion below would pass vacuously.
    if (isdst_now, isdst_before) != (0, 1):
        raise AssertionError(
            "TZ=%s did not place the child just after a DST fall-back "
            "(isdst now=%s, 45min ago=%s) -- the ambiguous-wall-clock case "
            "would not be exercised and this test would be vacuous."
            % (tz, isdst_now, isdst_before))

    with _scratch_db() as db:
        _run(_LEGACY, tz, db, 10)
        _, res = _run(_READER, tz, db)
    check(res == "True",
          "legacy naive row written 10s ago reads ONLINE inside a DST fold, as "
          "the pre-fix comparison did on the same single host (got %s)" % res)


def test_aware_expiry_path_is_exercised():
    """Belt-and-braces: the *shipped* path is an aware stamp past the TTL.

    The suite's `_expire_worker()` helper used to hand-write a naive stamp, so
    after this fix every reap/offline test would have gone down the legacy
    fallback instead of the real path.

    This pins `heartbeat()`. `_expire_worker()` itself is pinned separately by
    `test_expire_worker_helper_writes_an_aware_stamp` -- an earlier version of
    this docstring claimed *this* test covered it, which was false: reverting
    the helper to a naive stamp left the whole suite green.
    """
    with _scratch_db() as db:
        # A real heartbeat from a worker whose clock is TTL+60s back, so the
        # stamp is exactly what a genuinely expired worker would have written.
        _run(_WRITER, "UTC", db, age=jobs.WORKER_TTL_SECONDS + 60)
        _, stored = _run(_READ_RAW, "UTC", db)
        _, res = _run(_READER, "UTC", db)
    parsed = datetime.fromisoformat(stored)
    check(parsed.tzinfo is not None,
          "heartbeat() stores an offset-aware stamp, so the expiry path under "
          "test is the shipped one and not the legacy fallback (stored %r)" % stored)
    check(res == "False", "an offset-aware stamp past the TTL reads OFFLINE (got %s)" % res)


def test_expire_worker_helper_writes_an_aware_stamp():
    """`test_ff_connect_flow._expire_worker()` must stamp aware, and this must fail if it stops.

    A naive stamp there would still expire the worker, so that file's own
    reap/offline assertions stay green either way -- they would just silently
    run down `worker_online()`'s legacy naive-compatibility branch, leaving the
    shipped aware path uncovered. No assertion inside that file can catch this,
    which is why the stamp is pinned from here by reading the source.

    (Before VEN-166 that file's `check()` only appended to a list, so none of
    its assertions could fail under pytest at all. It raises now, but this pin
    is still required for the path-coverage reason above.)
    """
    src = (Path(REPO) / "tests" / "test_ff_connect_flow.py").read_text()
    body = src.split("def _expire_worker(")[1].split("\ndef ")[0]
    check("timezone.utc" in body,
          "_expire_worker() builds its stamp with timezone.utc, so the reap/offline "
          "tests exercise the shipped aware path rather than the legacy fallback")


def main():
    for fn in (test_live_worker_reads_online_in_every_timezone_pair,
               test_dead_worker_reads_offline_in_every_timezone_pair,
               test_eastward_dead_worker_is_not_reported_online,
               test_legacy_naive_row_does_not_crash_the_reader,
               test_legacy_naive_row_survives_an_ambiguous_wall_clock,
               test_aware_expiry_path_is_exercised,
               test_expire_worker_helper_writes_an_aware_stamp):
        print(fn.__name__)
        try:
            fn()
        except AssertionError:
            pass          # `check` already recorded and printed it
    if _FAILURES:
        print("\n%d FAILED" % len(_FAILURES))
        sys.exit(1)
    print("\nall ok")


if __name__ == "__main__":
    main()
