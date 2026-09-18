"""VEN-162: no background thread may outlive the test that spawned it.

Importing `dashboard` used to start the `autopilot-scheduler` daemon thread as
an *import side effect*, and three request routes start the `outbox-drainer`
the same way. Both outlive the test that triggered them: the scheduler polls
every 60 s for the rest of the session, the drainer every 5 s for at least 30 s.
Meanwhile the fixtures repoint `db.DB_PATH` per test, so the surviving thread
keeps opening connections to whichever temp database is *now* current and
writing into it — which is why unrelated tests failed sporadically, a different
one each run (`duplicate column name: digest_hour`, a queued row a later test
still expected to be queued).

Two halves, and the second is the load-bearing one:

* with `DISABLE_BACKGROUND_AGENTS` set, nothing spawns — the isolation fix;
* with it **unset**, the scheduler still starts on import — the production
  default. Every hosted entrypoint (`Procfile`, `render.yaml`, `Dockerfile`)
  boots by importing `dashboard`, so a gate accidentally welded shut would kill
  autopilot silently on every deploy. That has happened here before; this file
  is the thing that would notice.
"""
import ast
import importlib.util
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import automation  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402

_FLAG = "DISABLE_BACKGROUND_AGENTS"


@pytest.fixture(autouse=True)
def reset_spawn_latches():
    """`start_*` sets a module global that only the real loop's `finally` clears.

    These tests substitute the loop, so nothing clears it for them. Reset on
    both sides: a latch left set here would silently suppress a spawn in a later
    test in this file and turn a red assertion green.
    """
    automation._scheduling = False
    automation._draining = False
    yield
    automation._scheduling = False
    automation._draining = False


# --- the gate, asserted at the function ------------------------------------
# At the function, not at the call sites: `start_drainer` has six non-test
# callers (three of them request routes) and `start_scheduler` two, one of which
# — the `/automations` POST that turns autopilot on — is not gated on
# `playwright_available()` either. Gating callers only moves the leak.

def test_the_suite_itself_runs_with_background_agents_disabled():
    """`tests/conftest.py` must set the flag before the first test module is
    imported — a fixture would be too late, because `import dashboard` is what
    starts the thread.

    Deliberately strict: running the escape hatch (`DISABLE_BACKGROUND_AGENTS=0
    pytest`, which exists to reproduce the old flake) is expected to fail this
    one test. In that mode the suite really is non-isolated, and saying so is
    the correct signal.
    """
    assert automation.background_agents_enabled() is False, (
        "the test process is running with background agents ENABLED; "
        f"{_FLAG}={os.environ.get(_FLAG)!r}"
    )


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " 1 "])
def test_start_scheduler_spawns_nothing_when_disabled(monkeypatch, value):
    monkeypatch.setenv(_FLAG, value)
    reached = []
    monkeypatch.setattr(automation, "_scheduler_loop", lambda site: reached.append(site))

    assert automation.start_scheduler("furnishedfinder") is False
    assert reached == []
    assert "autopilot-scheduler" not in _thread_names()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " 1 "])
def test_start_drainer_spawns_nothing_when_disabled(monkeypatch, value):
    monkeypatch.setenv(_FLAG, value)
    reached = []
    monkeypatch.setattr(automation, "_drain_loop", lambda site: reached.append(site))

    assert automation.start_drainer("furnishedfinder") is False
    assert reached == []
    assert "outbox-drainer" not in _thread_names()


def test_the_gate_still_lets_the_scheduler_start_when_the_flag_is_unset(monkeypatch):
    """The anti-weld half, in-process. Substituting the loop (an *input* to the
    spawn) rather than `start_scheduler` itself keeps the function under test
    real, while stopping a 60 s poller from actually being left running."""
    monkeypatch.delenv(_FLAG, raising=False)
    started = _spawn_probe(monkeypatch, "_scheduler_loop")

    assert automation.start_scheduler("furnishedfinder") is True
    assert started.wait(5), "no scheduler thread ran"
    assert started.name == "autopilot-scheduler"
    assert started.daemon is True
    assert started.site == "furnishedfinder"


def test_the_gate_still_lets_the_drainer_start_when_the_flag_is_unset(monkeypatch):
    monkeypatch.delenv(_FLAG, raising=False)
    started = _spawn_probe(monkeypatch, "_drain_loop")

    assert automation.start_drainer("furnishedfinder") is True
    assert started.wait(5), "no drainer thread ran"
    assert started.name == "outbox-drainer"
    assert started.daemon is True


def test_a_refused_spawn_does_not_consume_the_idempotence_latch(monkeypatch):
    """The gate has to be checked *before* `_draining`/`_scheduling` is taken.

    Checked after, a disabled call would set the latch and never start the loop
    that clears it — so the first *enabled* call in that process would be told
    "already running" and nothing would ever drain. Order matters, so pin it.
    """
    monkeypatch.setenv(_FLAG, "1")
    assert automation.start_drainer("furnishedfinder") is False
    assert automation.start_scheduler("furnishedfinder") is False
    assert automation._draining is False
    assert automation._scheduling is False

    monkeypatch.delenv(_FLAG, raising=False)
    drain = _spawn_probe(monkeypatch, "_drain_loop")
    sched = _spawn_probe(monkeypatch, "_scheduler_loop")
    assert automation.start_drainer("furnishedfinder") is True, (
        "a refused spawn left the drainer latch set")
    assert automation.start_scheduler("furnishedfinder") is True, (
        "a refused spawn left the scheduler latch set")
    assert drain.wait(5) and sched.wait(5)


# --- the import side effect, in a real subprocess ---------------------------
# `subprocess` is not optional: `dashboard` is already in `sys.modules` by the
# time this file runs, so its import side effect cannot be observed again
# in-process.

_PROBE = """
import sys, threading
sys.path.insert(0, %r)
import dashboard  # noqa: F401 -- the import IS the thing under test
print(sorted(t.name for t in threading.enumerate()
             if t is not threading.main_thread() and t.is_alive()))
"""


def test_importing_dashboard_starts_no_thread_when_disabled(tmp_path):
    names = _import_dashboard(tmp_path, {_FLAG: "1"})
    assert names == [], f"import left threads running: {names}"


@pytest.mark.skipif(
    importlib.util.find_spec("playwright") is None,
    reason="no Playwright in this interpreter, so the scheduler is gated off "
           "for an unrelated reason and this would pass vacuously",
)
def test_importing_dashboard_still_starts_the_scheduler_by_default(tmp_path):
    """The production default, asserted the way production gets it: an import.

    If this ever fails, autopilot is dead on every hosted deploy — that is the
    failure mode this whole change is shaped around avoiding.
    """
    names = _import_dashboard(tmp_path, {})
    assert "autopilot-scheduler" in names, (
        f"the scheduler no longer starts on import; threads seen: {names}")


def _import_dashboard(tmp_path, overrides):
    root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    for key in (_FLAG, "FORCE_WORKER_QUEUE", "DATABASE_URL"):
        env.pop(key, None)  # the other two gates on the same code path
    env["SQLITE_PATH"] = str(tmp_path / "probe.db")
    env.setdefault("SECRET_KEY", "test-secret")
    env.setdefault("FF_CRED_KEY", "c9jwUi0L-fUjf3wjbq74M0lK3ah7fmEfGhjxZ7RehQk=")
    env.update(overrides)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE % root],
        env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"probe failed:\n{proc.stdout}\n{proc.stderr}"
    return ast.literal_eval(proc.stdout.strip().splitlines()[-1])


# --- the race the leaked thread was hitting --------------------------------

def test_concurrent_first_connections_do_not_collide_on_add_column(tmp_path, monkeypatch):
    """SQLite runs the DDL on *every* connection, and the ADD COLUMN migrations
    are check-then-act: read the column list, ALTER what is missing. Two threads
    opening the first connection to a fresh file both read the same list and
    both ALTER — `duplicate column name: <whichever lost>`. That is the reported
    `digest_hour` symptom, and it is a real concurrency bug beyond the tests:
    production serves on `gunicorn --threads 8` over the same SQLite path.

    The leaked scheduler thread was simply the only other writer that made it
    reachable. Unserialised, this fails; the per-process DDL lock closes it.
    """
    failures = _ddl_race_failures(tmp_path, monkeypatch, trials=60, threads=4)
    assert failures == [], f"{len(failures)}/60 trials raced: {failures[:3]}"


def test_a_forked_child_does_not_inherit_a_held_ddl_lock(tmp_path, monkeypatch):
    """`fork()` copies the lock in whatever state it was in — held by a thread
    that does not exist in the child, so held forever. `_reset_schema_state`
    already rebuilds the Postgres schema lock for that reason and runs in the
    child via `os.register_at_fork`; the SQLite DDL lock has to be rebuilt there
    too, because it is taken on *every* connection, so a child inheriting it
    held would hang on its first query rather than on some later migration.
    """
    held, release = threading.Event(), threading.Event()

    def hold_it():
        with db._SQLITE_DDL_LOCK:
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold_it, name="ven162-lock-holder")
    holder.start()
    try:
        assert held.wait(5), "could not take the DDL lock"
        db._reset_schema_state()  # what the child runs immediately after fork

        monkeypatch.setattr(db, "DB_PATH", tmp_path / "child.db")
        connected = threading.Event()

        def child_work():
            with config._conn():
                connected.set()

        worker = threading.Thread(target=child_work, name="ven162-child")
        worker.start()
        assert connected.wait(10), (
            "a child inherited a DDL lock still held by a thread it does not have")
        worker.join(10)
    finally:
        release.set()
        holder.join(10)


def _ddl_race_failures(tmp_path, monkeypatch, *, trials, threads):
    failures = []
    for trial in range(trials):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / f"race{trial}.db")
        barrier = threading.Barrier(threads)
        errors = []

        def open_one():
            barrier.wait()
            try:
                with config._conn():
                    pass
            except Exception as exc:  # noqa: BLE001 -- any error is a failure
                errors.append(repr(exc))

        workers = [threading.Thread(target=open_one) for _ in range(threads)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(30)
        failures.extend(errors)
    return failures


# --- helpers ---------------------------------------------------------------

def _thread_names():
    return sorted(t.name for t in threading.enumerate() if t.is_alive())


class _SpawnProbe:
    """Records what the spawned thread saw about itself, then returns."""

    def __init__(self):
        self._event = threading.Event()
        self.name = None
        self.daemon = None
        self.site = None

    def __call__(self, site):
        me = threading.current_thread()
        self.name, self.daemon, self.site = me.name, me.daemon, site
        self._event.set()

    def wait(self, timeout):
        return self._event.wait(timeout)


def _spawn_probe(monkeypatch, loop_attr):
    probe = _SpawnProbe()
    monkeypatch.setattr(automation, loop_attr, probe)
    return probe
