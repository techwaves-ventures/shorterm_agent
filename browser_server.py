"""Authenticated local browser facade server.

An independent HTTP server that fronts the already-hardened Playwright runner so
another service can drive FurnishedFinder browser work — log in, wait for the
human to hand back the login OTP, collect leads/messages, and send an approved
reply — over a small fixed set of endpoints. It shares the Shorterm DB/state and
reuses `runner.py`, `sites/furnishedfinder.py`, and `storage.py` unchanged;
nothing here talks to Playwright directly.

Security posture (mirrors `chrome_task_server.py` via the shared `sig_auth`):
  - bind host is explicit in the service/env; public binds still require auth
  - no generated docs/OpenAPI surface (plain Flask)
  - bearer token plus HMAC signature, timestamp, and nonce replay protection
  - fail closed when auth env is missing
  - OTP handoff is human-in-the-loop: the code is routed to the waiting run and
    is never logged, echoed back, or persisted

Endpoints (all POST, JSON body, signed):
  - /v1/login    -> start an FF login + scrape run for a tenant
  - /v1/state    -> current run-state snapshot for a tenant
  - /v1/otp      -> submit the login OTP the human received back to the run
  - /v1/leads    -> collected leads (dedup store) for a tenant
  - /v1/messages -> collected messages (dedup store) for a tenant
  - /v1/reply    -> send an approved reply to a lead/message
  - /healthz     -> authenticated liveness probe
"""
from __future__ import annotations

import logging
import os
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify

load_dotenv()

import runner
import storage
from sig_auth import SignatureAuth, json_error as _json_error

log = logging.getLogger("browser_server")

SITE = "furnishedfinder"
DEFAULT_TENANT = "1"

HOST = os.getenv("BROWSER_SERVER_HOST", "127.0.0.1")
PORT = int(os.getenv("BROWSER_SERVER_PORT", "6767"))
AUTH_TOKEN = os.getenv("BROWSER_SERVER_BEARER_TOKEN", "")
HMAC_KEY = os.getenv("BROWSER_SERVER_HMAC_KEY", "")
TIMESTAMP_TOLERANCE_SECONDS = int(os.getenv("BROWSER_SERVER_TIMESTAMP_TOLERANCE_SECONDS", "300"))
# Larger than the wake server's 2 KiB cap: a /v1/reply body carries the full
# reply text, which can run to a paragraph or two.
MAX_BODY_BYTES = int(os.getenv("BROWSER_SERVER_MAX_BODY_BYTES", "16384"))
# Cap how many stored items a single collect call can return.
MAX_LIMIT = 200
DEFAULT_LIMIT = 20

_auth = SignatureAuth(
    token=AUTH_TOKEN,
    key=HMAC_KEY,
    tolerance_seconds=TIMESTAMP_TOLERANCE_SECONDS,
    max_body_bytes=MAX_BODY_BYTES,
    unconfigured_message="browser server auth is not configured",
)

app = Flask(__name__)


def _tenant(payload: dict[str, Any]) -> str:
    """The tenant a request acts on. Defaults to the operator ('1'); coerced to a
    string so JSON numbers and strings key the same tenant."""
    tid = payload.get("tenant_id", DEFAULT_TENANT)
    tid = str(tid).strip()
    return tid or DEFAULT_TENANT


def _limit(payload: dict[str, Any]) -> int:
    try:
        n = int(payload.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        n = DEFAULT_LIMIT
    return max(1, min(n, MAX_LIMIT))


def _collect(tenant_id: str, kind: str, limit: int) -> list[dict]:
    """Stored items of a kind for a tenant, each tagged with its kind and the
    responder's decision (mirrors the dashboard's `_recent`)."""
    responses = storage.get_responses(tenant_id, SITE)
    items = storage.get_recent(tenant_id, SITE, kind, limit)
    for it in items:
        it["kind"] = kind
        it["response"] = responses.get(it.get("id"))
    return items


def _item_by_id(tenant_id: str, item_id: str) -> dict | None:
    """Find a lead or message by id within this tenant, tagging its kind."""
    for kind in ("lead", "message"):
        for it in storage.get_recent(tenant_id, SITE, kind, MAX_LIMIT):
            if it.get("id") == item_id:
                it["kind"] = kind
                return it
    return None


@app.get("/healthz")
def healthz():
    _payload, error = _auth.verify()
    if error:
        return error
    return jsonify({"ok": True, "service": "shorterm-browser-server"})


@app.post("/v1/login")
def login():
    """Start an FF login + scrape run for the tenant. The run drives the browser
    to the email step and then blocks waiting for the human-supplied OTP — poll
    /v1/state for `waiting_for_otp`, then hand the code back via /v1/otp."""
    payload, error = _auth.verify()
    if error:
        return error
    state = runner.start_scrape(_tenant(payload))
    return jsonify({"ok": True, "state": state})


@app.post("/v1/state")
def state():
    payload, error = _auth.verify()
    if error:
        return error
    return jsonify({"ok": True, "state": runner.get_state(_tenant(payload))})


@app.post("/v1/otp")
def otp():
    """Route the login OTP the human received back to the tenant's waiting run.
    The code is never logged or persisted here."""
    payload, error = _auth.verify()
    if error:
        return error
    tenant_id = _tenant(payload)
    code = payload.get("code", "")
    if not isinstance(code, str):
        return _json_error(400, "code must be a string")
    ok = runner.submit_otp(tenant_id, code)
    return jsonify({"ok": ok, "state": runner.get_state(tenant_id)})


@app.post("/v1/leads")
def leads():
    payload, error = _auth.verify()
    if error:
        return error
    tenant_id = _tenant(payload)
    return jsonify({"ok": True, "leads": _collect(tenant_id, "lead", _limit(payload))})


@app.post("/v1/messages")
def messages():
    payload, error = _auth.verify()
    if error:
        return error
    tenant_id = _tenant(payload)
    return jsonify({"ok": True, "messages": _collect(tenant_id, "message", _limit(payload))})


@app.post("/v1/reply")
def reply():
    """Send an approved reply to a stored lead/message. `item_id` locates the
    stored item within the tenant; `text` is the (possibly human-edited) reply."""
    payload, error = _auth.verify()
    if error:
        return error
    tenant_id = _tenant(payload)
    item_id = payload.get("item_id", "")
    text = payload.get("text", "")
    if not isinstance(item_id, str) or not item_id:
        return _json_error(400, "item_id is required")
    if not isinstance(text, str) or not text.strip():
        return _json_error(400, "text is required")
    item = _item_by_id(tenant_id, item_id)
    if not item:
        return _json_error(404, "item not found")
    state = runner.send_reply(tenant_id, SITE, item, text)
    return jsonify({"ok": True, "state": state})


@app.errorhandler(404)
def not_found(_exc):
    return _json_error(404, "not found")


@app.errorhandler(405)
def method_not_allowed(_exc):
    return _json_error(405, "method not allowed")


@app.errorhandler(500)
def internal_error(_exc):
    log.exception("Unhandled browser server error")
    return _json_error(500, "internal server error")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not _auth.configured():
        log.error("BROWSER_SERVER_BEARER_TOKEN and BROWSER_SERVER_HMAC_KEY are required")
    app.run(host=HOST, port=PORT, debug=False)


if __name__ == "__main__":
    main()
