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

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

import jobs
import worker

log = logging.getLogger("chrome_task_server")

HOST = os.getenv("CHROME_TASK_HOST", "127.0.0.1")
PORT = int(os.getenv("CHROME_TASK_PORT", "6756"))
AUTH_TOKEN = os.getenv("CHROME_TASK_BEARER_TOKEN", "")
HMAC_KEY = os.getenv("CHROME_TASK_HMAC_KEY", "")
TIMESTAMP_TOLERANCE_SECONDS = int(os.getenv("CHROME_TASK_TIMESTAMP_TOLERANCE_SECONDS", "300"))
MAX_JOBS_PER_WAKE = int(os.getenv("CHROME_TASK_MAX_JOBS_PER_WAKE", "1"))
MAX_BODY_BYTES = 2048

_nonces: OrderedDict[str, float] = OrderedDict()
_nonces_lock = threading.Lock()
_wake_lock = threading.Lock()

app = Flask(__name__)


def _json_error(status: int, message: str):
    return jsonify({"ok": False, "error": message}), status


def _auth_configured() -> bool:
    return bool(AUTH_TOKEN and HMAC_KEY)


def _body_bytes() -> bytes:
    body = request.get_data(cache=True) or b""
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("request body too large")
    return body


def _body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _signature_payload(timestamp: str, nonce: str, method: str, path: str, body: bytes) -> bytes:
    msg = "\n".join([timestamp, nonce, method.upper(), path, _body_hash(body)])
    return msg.encode("utf-8")


def _expected_signature(timestamp: str, nonce: str, method: str, path: str, body: bytes) -> str:
    return hmac.new(
        HMAC_KEY.encode("utf-8"),
        _signature_payload(timestamp, nonce, method, path, body),
        hashlib.sha256,
    ).hexdigest()


def _check_nonce(nonce: str, now: float) -> bool:
    with _nonces_lock:
        cutoff = now - TIMESTAMP_TOLERANCE_SECONDS
        while _nonces and next(iter(_nonces.values())) < cutoff:
            _nonces.popitem(last=False)
        if nonce in _nonces:
            return False
        _nonces[nonce] = now
        return True


def require_auth() -> tuple[dict[str, Any] | None, Any | None]:
    if not _auth_configured():
        return None, _json_error(503, "chrome task auth is not configured")

    auth = request.headers.get("Authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, AUTH_TOKEN):
        return None, _json_error(401, "unauthorized")

    ts = request.headers.get("X-Shorterm-Timestamp", "")
    nonce = request.headers.get("X-Shorterm-Nonce", "")
    sig = request.headers.get("X-Shorterm-Signature", "")
    if not ts or not nonce or not sig:
        return None, _json_error(401, "missing signature headers")

    try:
        ts_int = int(ts)
    except ValueError:
        return None, _json_error(401, "invalid timestamp")

    now = time.time()
    if abs(now - ts_int) > TIMESTAMP_TOLERANCE_SECONDS:
        return None, _json_error(401, "stale timestamp")
    if not _check_nonce(nonce, now):
        return None, _json_error(401, "replayed nonce")

    try:
        body = _body_bytes()
    except ValueError as exc:
        return None, _json_error(413, str(exc))

    expected = _expected_signature(ts, nonce, request.method, request.path, body)
    if not secrets.compare_digest(sig, expected):
        return None, _json_error(401, "invalid signature")

    payload: dict[str, Any] = {}
    if body:
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return None, _json_error(400, "invalid json")
        if not isinstance(payload, dict):
            return None, _json_error(400, "json body must be an object")
    return payload, None


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

    max_jobs = int(payload.get("max_jobs", 1) if payload else 1)
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
