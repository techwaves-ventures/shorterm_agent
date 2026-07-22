"""Authenticated local Chrome task wake server.

This is a narrow control plane for the off-Vercel browser worker. It does not
accept arbitrary URLs, scripts, or browser commands. A caller can only ask the
server to claim existing authorized DB jobs that the web app already enqueued.

Security posture:
  - bind host is explicit in the service/env; public binds still require auth
  - no generated docs/OpenAPI surface (plain Flask)
  - bearer token plus HMAC signature, timestamp, and nonce replay protection
  - fail closed when auth env is missing
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify

load_dotenv()

import jobs
import worker
from sig_auth import SignatureAuth, json_error as _json_error

log = logging.getLogger("chrome_task_server")

HOST = os.getenv("CHROME_TASK_HOST", "127.0.0.1")
PORT = int(os.getenv("CHROME_TASK_PORT", "6756"))
AUTH_TOKEN = os.getenv("CHROME_TASK_BEARER_TOKEN", "")
HMAC_KEY = os.getenv("CHROME_TASK_HMAC_KEY", "")
TIMESTAMP_TOLERANCE_SECONDS = int(os.getenv("CHROME_TASK_TIMESTAMP_TOLERANCE_SECONDS", "300"))
MAX_JOBS_PER_WAKE = int(os.getenv("CHROME_TASK_MAX_JOBS_PER_WAKE", "1"))
MAX_BODY_BYTES = 2048

_wake_lock = threading.Lock()

_auth = SignatureAuth(
    token=AUTH_TOKEN,
    key=HMAC_KEY,
    tolerance_seconds=TIMESTAMP_TOLERANCE_SECONDS,
    max_body_bytes=MAX_BODY_BYTES,
    unconfigured_message="chrome task auth is not configured",
)

app = Flask(__name__)


def require_auth() -> tuple[dict[str, Any] | None, Any | None]:
    return _auth.verify()


@app.get("/healthz")
def healthz():
    _payload, error = require_auth()
    if error:
        return error
    return jsonify({"ok": True, "service": "shorterm-chrome-task"})


@app.post("/v1/wake")
def wake():
    payload, error = require_auth()
    if error:
        return error

    try:
        max_jobs = int(payload.get("max_jobs", 1) if payload else 1)
    except (TypeError, ValueError):
        return _json_error(400, "max_jobs must be an integer")
    max_jobs = max(0, min(max_jobs, MAX_JOBS_PER_WAKE))
    if max_jobs < 1:
        return _json_error(400, "max_jobs must be at least 1")

    # Serialize wake-triggered claims so a burst of authenticated requests does
    # not start multiple browser jobs in the same process.
    ran = 0
    with _wake_lock:
        worker_id = f"chrome-task-server:{os.getpid()}"
        for _ in range(max_jobs):
            if not worker.run_once(worker_id):
                break
            ran += 1

    return jsonify({"ok": True, "claimed": ran, "worker_online": jobs.worker_online()})


@app.errorhandler(404)
def not_found(_exc):
    return _json_error(404, "not found")


@app.errorhandler(405)
def method_not_allowed(_exc):
    return _json_error(405, "method not allowed")


@app.errorhandler(500)
def internal_error(_exc):
    log.exception("Unhandled Chrome task server error")
    return _json_error(500, "internal server error")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not _auth_configured():
        log.error("CHROME_TASK_BEARER_TOKEN and CHROME_TASK_HMAC_KEY are required")
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
