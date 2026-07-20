"""Off-Vercel scrape worker: claims queued FurnishedFinder jobs and runs them.

The Vercel-hosted dashboard can't run Playwright, so "Check now" there enqueues
a job (see jobs.py). This worker runs on a host that DOES have Playwright +
Chromium and shares the same `DATABASE_URL`. It:

  1. heartbeats so the dashboard can tell a worker is online,
  2. claims the oldest queued job,
  3. runs the existing live scrape (check_leads.run_scrape) for that tenant,
  4. bridges the tenant's OTP back through the shared DB (jobs.consume_otp),
  5. writes UI-safe status/results home and marks the FF account connected on
     success (or a friendly error on failure).

Run it anywhere with the browser installed (a small VM, Render/Fly worker, the
systemd unit, or locally):

    DATABASE_URL='postgres://…' FF_CRED_KEY=… python worker.py
    python worker.py --once        # drain the queue once and exit (tests/cron)

Tenant isolation and secret hygiene are preserved: jobs are per-tenant, OTP
codes are encrypted and consumed once, and no OTP/credential/lead PII is logged.
"""
import argparse
import logging
import os
import socket
import sys
import threading
import time

from dotenv import load_dotenv

load_dotenv()

import check_leads
import ff_account
import jobs
import runner
from sites import furnishedfinder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("worker")

POLL_SECONDS = int(os.getenv("WORKER_POLL_SECONDS", "5"))
OTP_WAIT_SECONDS = 600  # how long a run waits for the tenant to submit their code
HEARTBEAT_SECONDS = 15  # keep worker_online() true even during a long OTP wait


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _start_heartbeat(worker_id: str, stop_event: "threading.Event") -> "threading.Thread":
    """Beat continuously (daemon) so jobs.worker_online() stays true across a long
    process()/OTP wait — the reaper keys staleness off this liveness signal."""
    def beat() -> None:
        while not stop_event.wait(HEARTBEAT_SECONDS):
            try:
                jobs.heartbeat(worker_id)
            except Exception:
                log.exception("Heartbeat failed")
    t = threading.Thread(target=beat, name="worker-heartbeat", daemon=True)
    t.start()
    return t


def _otp_provider(job_id: int):
    """Block until the tenant submits an OTP for this job (or time out).

    Polls the shared DB for a code the dashboard wrote via jobs.submit_otp. The
    code is consumed (cleared) on read, so it lives at rest only briefly and is
    never logged."""
    def provider() -> str | None:
        deadline = time.time() + OTP_WAIT_SECONDS
        while time.time() < deadline:
            code = jobs.consume_otp(job_id)
            if code:
                log.info("Received OTP for job %s via shared DB", job_id)
                return code
            time.sleep(2)
        return None

    return provider


def _status_cb(job_id: int):
    """Bridge the scraper's progress callbacks to the job row (UI-safe text)."""
    def cb(state: str, message: str = "") -> None:
        if state == "waiting_for_otp":
            js = jobs.WAITING_FOR_OTP
        else:  # launching / checking / done → running (terminal set explicitly)
            js = jobs.RUNNING
        try:
            jobs.set_status(job_id, js, message or state)
        except Exception:
            log.exception("Failed to write job status")

    return cb


def process(job: dict) -> None:
    job_id = job["id"]
    tenant_id = str(job["tenant_id"])
    log.info("Processing job %s for tenant %s", job_id, tenant_id)

    username = runner._ff_username(tenant_id)
    furnishedfinder.set_context(username, _otp_provider(job_id), _status_cb(job_id))
    _mark_ff(tenant_id, ff_account.VERIFYING)
    try:
        counts = check_leads.run_scrape(
            status_cb=_status_cb(job_id),
            on_new_items=runner._draft_new_items,
            tenant_id=tenant_id,
        )
        import json
        jobs.set_status(job_id, jobs.DONE, "Done — leads updated.", counts=json.dumps(counts))
        _mark_ff(tenant_id, ff_account.CONNECTED)
        log.info("Job %s done (counts=%s)", job_id, counts)
    except Exception as exc:
        # Log full detail server-side; show the customer a friendly message only.
        log.exception("Job %s failed", job_id)
        friendly = getattr(exc, "user_safe_message", None) or (
            "Couldn't verify your FurnishedFinder login. Please try Check now again."
        )
        jobs.set_status(job_id, jobs.ERROR, friendly)
        _mark_ff(tenant_id, ff_account.ERROR, error=friendly)
    finally:
        furnishedfinder.clear_context()


def _mark_ff(tenant_id: str, state: str, error: str | None = None) -> None:
    if str(tenant_id) == "1":
        return  # operator has no ff_accounts row
    try:
        ff_account.mark_state(tenant_id, state, error=error)
    except Exception:
        log.exception("Could not update FF account state to %s", state)


def run_once(worker_id: str) -> bool:
    """Claim and process one job. Returns True if a job ran, False if idle."""
    jobs.heartbeat(worker_id)
    job = jobs.claim_next(worker_id)
    if not job:
        return False
    process(job)
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="FurnishedFinder scrape worker")
    ap.add_argument("--once", action="store_true",
                    help="Drain the queue once and exit (default: poll forever).")
    args = ap.parse_args()

    if not check_leads.playwright_available():
        log.error(
            "Playwright is not installed in this environment — the worker cannot "
            "scrape. Install it: pip install -r requirements.txt && playwright "
            "install --with-deps chrome"
        )
        sys.exit(1)

    worker_id = _worker_id()
    log.info("Worker %s starting (poll=%ss, once=%s)", worker_id, POLL_SECONDS, args.once)

    jobs.heartbeat(worker_id)
    reaped = jobs.reap_stale(active_worker_id=worker_id)
    if reaped:
        log.info("Reaped %d stale job(s) orphaned before startup", reaped)
    _start_heartbeat(worker_id, threading.Event())  # daemon; dies with the process

    if args.once:
        while run_once(worker_id):
            pass
        log.info("Queue drained; exiting (--once).")
        return

    while True:
        try:
            ran = run_once(worker_id)
        except Exception:
            log.exception("Worker loop error; continuing")
            ran = False
        if not ran:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
