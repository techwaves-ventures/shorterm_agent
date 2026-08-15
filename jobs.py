"""Shared-DB scrape job queue for the Vercel ↔ worker split.

The Vercel-hosted Flask app can't run Playwright (no browser, read-only FS), so
"Check now" there enqueues a job into the shared Postgres instead of scraping
in-process. A separate worker (running on a host that *does* have Playwright +
Chromium and shares the same `DATABASE_URL`) claims queued jobs, runs the live
FurnishedFinder scrape, bridges the tenant's OTP back through this table, and
writes results + status home. See DEPLOY.md and worker.py.

Everything is scoped by `tenant_id`; OTP codes are encrypted at rest
(crypto.Fernet) and cleared the moment the worker consumes them — they are never
logged. Job `message` values are UI-safe (site/progress text only, no traveler
PII), and worker errors are stored as short friendly strings, not raw traces.

Timestamps are stored as ISO strings we control so worker-liveness math is done
in Python (portable across SQLite and Postgres, no SQL date arithmetic). Every
one of them is *absolute* (UTC, offset-carrying) because every one of them is
written on one host and read on another — see `_now_utc` and `_age_seconds`.
"""
from datetime import datetime, timezone

import crypto
import db

# Live states a job can be in. Terminal: done / error / canceled.
QUEUED = "queued"
RUNNING = "running"
WAITING_FOR_OTP = "waiting_for_otp"
DONE = "done"
ERROR = "error"
CANCELED = "canceled"

ACTIVE_STATES = (QUEUED, RUNNING, WAITING_FOR_OTP)

_COLS = (
    "id", "tenant_id", "kind", "status", "message",
    "counts", "worker_id", "created_at", "updated_at",
)

# A worker is considered online if it heartbeated within this window. The worker
# heartbeats continuously (a background thread, even mid-scrape/OTP-wait), so a
# lack of heartbeat within this window reliably means the worker crashed/stopped.
WORKER_TTL_SECONDS = 90
# After a failed login/check, do not immediately create another browser job.
# This prevents repeated Check now clicks from spamming FurnishedFinder magic
# login emails while still allowing an intentional retry after a short pause.
ERROR_RETRY_COOLDOWN_SECONDS = 120
# Absolute backstop: a job stuck in an active browser state longer than this is
# reaped even if a worker still heartbeats (e.g. a wedged/hung run). Must exceed
# a legitimate run + the OTP wait (OTP_WAIT_SECONDS=600) so live runs aren't
# killed; staleness is normally caught far sooner by worker-liveness/ownership.
MAX_ACTIVE_JOB_SECONDS = 1800

# The browser-bound states a run passes through. A job left in one of these after
# a worker restart/crash/timeout is what strands the dashboard on "Checking…".
_BROWSER_STATES = (RUNNING, WAITING_FOR_OTP)

# UI-safe copy shown when the reaper fails a stranded job.
STALE_JOB_MESSAGE = (
    "The check stopped before it finished (the worker may have restarted). "
    "Click Check now to try again."
)


def _now_utc() -> str:
    """An *absolute* stamp. Every timestamp in this table crosses a host boundary.

    Unlike `outbox`, where only one column is read by a different process than
    wrote it, there is no naive-safe column here: this whole module exists to let
    the web dyno and the worker VM talk through one shared `DATABASE_URL`, and
    the worker may sit in any timezone while the web host runs UTC.

      * `ff_worker.last_seen` is written ONLY by the worker (`heartbeat`) and read
        ONLY by the web host (`worker_online`). With WORKER_TTL_SECONDS=90, *any*
        nonzero offset breaks it: westward the worker reads permanently offline
        and `reap_stale` kills live scrapes on every dashboard poll; eastward it
        reads permanently online and crash recovery never fires.
      * `ff_jobs.created_at` is written by the web host at `enqueue` and read by
        the worker at startup (`worker.py` -> `reap_stale(active_worker_id=...)`).
        Westward the MAX_ACTIVE_JOB_SECONDS backstop silently never trips, so a
        wedged-but-heartbeating job hangs forever with no route out.
      * `ff_jobs.updated_at` is written by the worker (`set_status`) and read by
        the web host (`_cooldown_remaining`). Westward the FurnishedFinder
        login-email burst guard is defeated; eastward it surfaces an absurd
        cooldown ("please wait 43319s") that locks the tenant out of retrying.

    So the rule here is simpler than in `outbox`: one helper, used for every
    write. Nothing in this table is compared as a *local wall clock* (no
    quiet-hours semantics, and ordering is by `id`, never by timestamp), so there
    is no reason to keep a naive writer around. VEN-142.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_seconds(value: str | None) -> float | None:
    """How long ago `value` happened, in absolute seconds, or None if unreadable.

    Stamps written by `_now_utc` carry their offset and are compared absolutely,
    which is the whole point. A *naive* value is a row written before this module
    stamped absolute (or by a not-yet-upgraded host mid-rollout); it names a wall
    clock on an unknown host, so it is read as the reader's own local time.

    That is exactly what this code did before VEN-142, and reading it any other
    way would be a regression rather than a fix:

      * Treating naive as UTC would shift every legacy row by the reader's offset
        — on the ordinary single-host deploy, where naive stamps are *correct*
        today, that would newly break the cooldown and reap live jobs.
      * Declining to judge it (returning None, as `outbox.reclaim_stuck_sending`
        does for its own column) is unsafe *here*, because for `created_at` this
        age IS the last line of defence: None skips the wedged-job backstop and
        strands the tenant on "Checking…" forever, which is worse than base.

    Legacy rows therefore behave precisely as they did on the previous release,
    and converge on their own — the worker re-stamps `last_seen` within seconds,
    and job rows are short-lived.
    """
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()  # naive == this host's wall clock (pre-VEN-142)
    return (datetime.now(timezone.utc) - stamp).total_seconds()


def _conn() -> db.Conn:
    c = db.connect()
    c.execute(
        """CREATE TABLE IF NOT EXISTS ff_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'scrape',
            status TEXT NOT NULL DEFAULT 'queued',
            message TEXT,
            otp_enc TEXT,
            counts TEXT,
            worker_id TEXT,
            created_at TEXT,
            updated_at TEXT
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS ff_worker (
            id INTEGER PRIMARY KEY,
            worker_id TEXT,
            last_seen TEXT
        )"""
    )
    return c


def _row_to_dict(row) -> dict | None:
    if not row:
        return None
    return dict(zip(_COLS, row))


_SELECT = f"SELECT {', '.join(_COLS)} FROM ff_jobs"


# ---------------------------------------------------------------------------
# Producer side (the web app)
# ---------------------------------------------------------------------------


def _reconcile_ff_error(tenant_id, message: str) -> None:
    """Mirror a reaped/failed job into the tenant's FF account state so it never
    lingers in `verifying`. No-op for the operator ('1', no ff_accounts row).
    Imported lazily to keep jobs.py import-cycle-free."""
    if str(tenant_id) == "1":
        return
    try:
        import ff_account
        ff_account.mark_state(str(tenant_id), ff_account.ERROR, error=message)
    except Exception:
        pass


def _fail_stale_job(job: dict, message: str) -> None:
    set_status(job["id"], ERROR, message)
    _reconcile_ff_error(job.get("tenant_id"), message)


def reap_stale(active_worker_id: str | None = None) -> int:
    """Fail jobs stranded in a browser state after a worker restart/crash/timeout.

    Idempotent and cheap; called lazily from the producer/UI paths so DB truth
    and the dashboard agree without a separate cron. A RUNNING/WAITING_FOR_OTP job
    is failed when ANY of:
      * `active_worker_id` is given (worker startup) and the job is owned by a
        DIFFERENT worker id — an orphan from the previous process (single-worker
        deploy), caught immediately on restart (RestartSec=5);
      * no worker heartbeated within WORKER_TTL_SECONDS — the worker crashed and
        hasn't returned (the dashboard's own /api/status poll drives this);
      * the job has been active longer than MAX_ACTIVE_JOB_SECONDS — backstop for
        a wedged worker that still heartbeats.
    Returns the number of jobs reaped.
    """
    online = worker_online()
    with _conn() as c:
        placeholders = ",".join("?" * len(_BROWSER_STATES))
        rows = c.execute(
            f"{_SELECT} WHERE status IN ({placeholders})", _BROWSER_STATES
        ).fetchall()
    reaped = 0
    for row in rows:
        job = _row_to_dict(row)
        if not job:
            continue
        owner = job.get("worker_id")
        age = _age_seconds(job.get("created_at"))
        orphan = active_worker_id is not None and owner and owner != active_worker_id
        if orphan or not online or (age is not None and age > MAX_ACTIVE_JOB_SECONDS):
            _fail_stale_job(job, STALE_JOB_MESSAGE)
            reaped += 1
    return reaped


def _cooldown_remaining(recent: dict | None) -> int:
    """Seconds a tenant must wait before a fresh login job is allowed, based on
    their most recent errored attempt. 0 when there's no active cooldown.

    The cooldown exists to stop repeated Check-now clicks from bursting real
    FurnishedFinder login emails, so it only applies to errors from an actual
    browser/login attempt. A reaper-induced error (STALE_JOB_MESSAGE) means the
    worker crashed/restarted before finishing — no login/email happened — so the
    user must be able to retry immediately (acceptance criterion #1)."""
    if not recent or recent.get("status") != ERROR:
        return 0
    if (recent.get("message") or "") == STALE_JOB_MESSAGE:
        return 0
    age = _age_seconds(recent.get("updated_at"))
    if age is None or age >= ERROR_RETRY_COOLDOWN_SECONDS:
        return 0
    return int(ERROR_RETRY_COOLDOWN_SECONDS - age)


def enqueue(tenant_id: str, kind: str = "scrape") -> dict:
    """Queue a scrape job for a tenant, or return the tenant's already-active job.

    At most one active job per tenant: clicking "Check now" twice coalesces onto
    the same run instead of stacking browser jobs.
    """
    tenant_id = str(tenant_id)
    existing = get_active(tenant_id)   # get_active() reaps stale jobs first
    if existing:
        return existing
    recent = latest(tenant_id)
    remaining = _cooldown_remaining(recent)
    if remaining > 0:
        # Do not fire another login (another FF email) yet — hand back the recent
        # errored job so the UI shows the cooldown message from public_state.
        throttled = dict(recent)
        throttled["cooldown_remaining"] = remaining
        return throttled
    now = _now_utc()
    with _conn() as c:
        job_id = db.insert_returning_id(
            c,
            "INSERT INTO ff_jobs (tenant_id, kind, status, message, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (tenant_id, kind, QUEUED, "Queued for the scraping worker.", now, now),
        )
    return latest(tenant_id) or {"id": job_id, "tenant_id": tenant_id, "status": QUEUED}


def get_active(tenant_id: str) -> dict | None:
    """The tenant's current in-flight job (queued/running/waiting), if any."""
    reap_stale()
    placeholders = ",".join("?" * len(ACTIVE_STATES))
    with _conn() as c:
        row = c.execute(
            f"{_SELECT} WHERE tenant_id=? AND status IN ({placeholders}) "
            "ORDER BY id DESC LIMIT 1",
            (str(tenant_id), *ACTIVE_STATES),
        ).fetchone()
    return _row_to_dict(row)


def latest(tenant_id: str) -> dict | None:
    """The tenant's most recent job of any status."""
    with _conn() as c:
        row = c.execute(
            f"{_SELECT} WHERE tenant_id=? ORDER BY id DESC LIMIT 1",
            (str(tenant_id),),
        ).fetchone()
    return _row_to_dict(row)


def submit_otp(tenant_id: str, code: str) -> bool:
    """Attach a one-time code to the tenant's active job so the worker can read
    it. Encrypted at rest. Returns False if there's no active job for the tenant
    (so a tenant can only feed their own run)."""
    code = (code or "").strip()
    if not code:
        return False
    job = get_active(str(tenant_id))
    if not job:
        return False
    enc = crypto.encrypt(code)
    with _conn() as c:
        c.execute(
            "UPDATE ff_jobs SET otp_enc=?, updated_at=? WHERE id=? AND tenant_id=?",
            (enc, _now_utc(), job["id"], str(tenant_id)),
        )
    return True


def cancel_active(tenant_id: str) -> None:
    """Cancel any in-flight job for a tenant (e.g. on disconnect)."""
    placeholders = ",".join("?" * len(ACTIVE_STATES))
    with _conn() as c:
        c.execute(
            f"UPDATE ff_jobs SET status=?, updated_at=? "
            f"WHERE tenant_id=? AND status IN ({placeholders})",
            (CANCELED, _now_utc(), str(tenant_id), *ACTIVE_STATES),
        )


# ---------------------------------------------------------------------------
# Consumer side (the worker)
# ---------------------------------------------------------------------------


def claim_next(worker_id: str) -> dict | None:
    """Atomically claim the oldest queued job, marking it running.

    The claim is a conditional UPDATE guarded on status=queued, so two workers
    racing for the same row can't both win (the loser's UPDATE affects 0 rows).
    Returns the claimed job dict, or None when the queue is empty.
    """
    with _conn() as c:
        row = c.execute(
            f"{_SELECT} WHERE status=? ORDER BY id ASC LIMIT 1", (QUEUED,)
        ).fetchone()
        job = _row_to_dict(row)
        if not job:
            return None
        cur = c.execute(
            "UPDATE ff_jobs SET status=?, worker_id=?, message=?, updated_at=? "
            "WHERE id=? AND status=?",
            (RUNNING, worker_id, "Starting…", _now_utc(), job["id"], QUEUED),
        )
        if getattr(cur, "rowcount", 1) == 0:
            return None  # lost the race to another worker
    job.update(status=RUNNING, worker_id=worker_id, message="Starting…")
    return job


def set_status(job_id: int, status: str, message: str | None = None,
               counts: str | None = None) -> None:
    """Update a job's status/message/counts. `message` must be UI-safe."""
    sets = ["status=?", "updated_at=?"]
    vals: list = [status, _now_utc()]
    if message is not None:
        sets.append("message=?")
        vals.append(message[:500])
    if counts is not None:
        sets.append("counts=?")
        vals.append(counts)
    vals.append(job_id)
    with _conn() as c:
        c.execute(f"UPDATE ff_jobs SET {', '.join(sets)} WHERE id=?", vals)


def consume_otp(job_id: int) -> str | None:
    """Read and CLEAR the pending OTP for a job. Returns the decrypted code once,
    then None (so a code is used at most once and doesn't linger at rest)."""
    with _conn() as c:
        row = c.execute(
            "SELECT otp_enc FROM ff_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not row or not row[0]:
            return None
        c.execute(
            "UPDATE ff_jobs SET otp_enc=NULL, updated_at=? WHERE id=?",
            (_now_utc(), job_id),
        )
        enc = row[0]
    return crypto.decrypt(enc)


def heartbeat(worker_id: str) -> None:
    """Record that a worker is alive (single-row liveness beacon)."""
    now = _now_utc()
    with _conn() as c:
        c.execute(
            """INSERT INTO ff_worker (id, worker_id, last_seen) VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 worker_id=excluded.worker_id, last_seen=excluded.last_seen""",
            (worker_id, now),
        )


def worker_online() -> bool:
    """True if a worker heartbeated within WORKER_TTL_SECONDS.

    `last_seen` is written on the worker and read here on the web host, so the
    comparison must be absolute — it goes through `_age_seconds` rather than
    subtracting wall clocks, which also means an unreadable stamp is handled in
    exactly one place instead of two. An unknown age reads as offline: that is
    what an empty `ff_worker` table already meant, and it fails towards crash
    recovery (a stranded job gets reaped) instead of stranding the dashboard.
    """
    with _conn() as c:
        row = c.execute("SELECT last_seen FROM ff_worker WHERE id=1").fetchone()
    if not row or not row[0]:
        return False
    age = _age_seconds(row[0])
    return age is not None and age <= WORKER_TTL_SECONDS


# ---------------------------------------------------------------------------
# UI projection
# ---------------------------------------------------------------------------

# Job status -> the dashboard's status vocabulary (idle | launching | checking |
# waiting_for_otp | done | error), matching the in-process runner state shape so
# the dashboard JS is identical on both the serverless and worker-host paths.
def public_state(tenant_id: str) -> dict:
    """A runner-compatible state snapshot derived from the tenant's latest job.

    Used on serverless hosts (no in-process Playwright) so "Check now", the
    status banner, and OTP entry all reflect the worker-backed run. Never leaks
    another tenant's data — it only reads this tenant's own job row.
    """
    reap_stale()
    job = latest(str(tenant_id))
    idle = {
        "status": "idle", "message": "", "counts": {}, "running": False,
        "tenant_id": str(tenant_id), "updated_at": None,
    }
    if not job:
        return idle
    st = job["status"]
    updated = job.get("updated_at")
    counts = _decode_counts(job.get("counts"))

    if st == QUEUED:
        if worker_online():
            msg = "Queued — the scraping worker is picking this up…"
        else:
            msg = ("Queued — waiting for the scraping worker to come online. "
                   "Your leads will load automatically once it runs.")
        return {"status": "launching", "message": msg, "counts": {},
                "running": True, "tenant_id": str(tenant_id), "updated_at": updated}
    if st == RUNNING:
        return {"status": "checking", "message": job.get("message") or "Checking FurnishedFinder…",
                "counts": {}, "running": True, "tenant_id": str(tenant_id), "updated_at": updated}
    if st == WAITING_FOR_OTP:
        return {"status": "waiting_for_otp",
                "message": job.get("message") or ("FurnishedFinder emailed you a login "
                    "code or a magic link. Paste the short code, or the entire "
                    "https://www.furnishedfinder.com/… link."),
                "counts": {}, "running": True, "tenant_id": str(tenant_id), "updated_at": updated}
    if st == DONE:
        return {"status": "done", "message": job.get("message") or "Done.",
                "counts": counts, "running": False, "tenant_id": str(tenant_id), "updated_at": updated}
    if st == ERROR:
        msg = job.get("message") or "The scrape didn't finish — please try again."
        remaining = _cooldown_remaining(job)
        if remaining > 0:
            msg = (f"{msg} Please wait {remaining}s before retrying so we don't "
                   "trigger extra FurnishedFinder login emails.")
        return {"status": "error", "message": msg, "counts": {},
                "running": False, "tenant_id": str(tenant_id), "updated_at": updated}
    return idle  # canceled / unknown


def _decode_counts(raw) -> dict:
    if not raw:
        return {}
    try:
        import json
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except (ValueError, TypeError):
        return {}
