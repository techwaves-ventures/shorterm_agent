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
in Python (portable across SQLite and Postgres, no SQL date arithmetic).
"""
from datetime import datetime, timedelta

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

# A worker is considered online if it heartbeated within this window.
WORKER_TTL_SECONDS = 90
# After a failed login/check, do not immediately create another browser job.
# This prevents repeated Check now clicks from spamming FurnishedFinder magic
# login emails while still allowing an intentional retry after a short pause.
ERROR_RETRY_COOLDOWN_SECONDS = 60


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _age_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(str(value))).total_seconds()
    except Exception:
        return None


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


def enqueue(tenant_id: str, kind: str = "scrape") -> dict:
    """Queue a scrape job for a tenant, or return the tenant's already-active job.

    At most one active job per tenant: clicking "Check now" twice coalesces onto
    the same run instead of stacking browser jobs.
    """
    tenant_id = str(tenant_id)
    existing = get_active(tenant_id)
    if existing:
        return existing
    recent = latest(tenant_id)
    recent_age = _age_seconds((recent or {}).get("updated_at"))
    if (
        recent
        and recent.get("status") == ERROR
        and recent_age is not None
        and recent_age < ERROR_RETRY_COOLDOWN_SECONDS
    ):
        return recent
    now = _now()
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
            (enc, _now(), job["id"], str(tenant_id)),
        )
    return True


def cancel_active(tenant_id: str) -> None:
    """Cancel any in-flight job for a tenant (e.g. on disconnect)."""
    placeholders = ",".join("?" * len(ACTIVE_STATES))
    with _conn() as c:
        c.execute(
            f"UPDATE ff_jobs SET status=?, updated_at=? "
            f"WHERE tenant_id=? AND status IN ({placeholders})",
            (CANCELED, _now(), str(tenant_id), *ACTIVE_STATES),
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
            (RUNNING, worker_id, "Starting…", _now(), job["id"], QUEUED),
        )
        if getattr(cur, "rowcount", 1) == 0:
            return None  # lost the race to another worker
    job.update(status=RUNNING, worker_id=worker_id, message="Starting…")
    return job


def set_status(job_id: int, status: str, message: str | None = None,
               counts: str | None = None) -> None:
    """Update a job's status/message/counts. `message` must be UI-safe."""
    sets = ["status=?", "updated_at=?"]
    vals: list = [status, _now()]
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
            (_now(), job_id),
        )
        enc = row[0]
    return crypto.decrypt(enc)


def heartbeat(worker_id: str) -> None:
    """Record that a worker is alive (single-row liveness beacon)."""
    now = _now()
    with _conn() as c:
        c.execute(
            """INSERT INTO ff_worker (id, worker_id, last_seen) VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 worker_id=excluded.worker_id, last_seen=excluded.last_seen""",
            (worker_id, now),
        )


def worker_online() -> bool:
    """True if a worker heartbeated within WORKER_TTL_SECONDS."""
    with _conn() as c:
        row = c.execute("SELECT last_seen FROM ff_worker WHERE id=1").fetchone()
    if not row or not row[0]:
        return False
    try:
        last = datetime.fromisoformat(row[0])
    except ValueError:
        return False
    return datetime.now() - last <= timedelta(seconds=WORKER_TTL_SECONDS)


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
                "message": job.get("message") or "Enter the code or magic login link FurnishedFinder emailed you.",
                "counts": {}, "running": True, "tenant_id": str(tenant_id), "updated_at": updated}
    if st == DONE:
        return {"status": "done", "message": job.get("message") or "Done.",
                "counts": counts, "running": False, "tenant_id": str(tenant_id), "updated_at": updated}
    if st == ERROR:
        return {"status": "error",
                "message": job.get("message") or "The scrape didn't finish — please try again.",
                "counts": {}, "running": False, "tenant_id": str(tenant_id), "updated_at": updated}
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
