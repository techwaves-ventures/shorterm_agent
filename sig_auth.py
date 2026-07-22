"""Shared HMAC request authentication for the off-Vercel browser control planes.

Both the Chrome task wake server (`chrome_task_server.py`) and the browser
facade server (`browser_server.py`) front local Playwright work that must never
be reachable without proof of possession of a shared secret. This module holds a
single hardened implementation of the bearer-token + HMAC-signature +
timestamp/nonce replay + body-size checks so the two servers cannot drift apart.

Security posture (unchanged from the original inline implementation):
  - bearer token compared in constant time
  - HMAC over (timestamp, nonce, METHOD, path, sha256(body)) — binds the request
  - timestamp tolerance window rejects stale/pre-dated requests
  - single-use nonces (pruned by the tolerance window) reject replays
  - body-size cap rejects oversized bodies before JSON parsing
  - fail closed when the token/key are not configured
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import OrderedDict
from typing import Any

from flask import jsonify, request


def json_error(status: int, message: str):
    return jsonify({"ok": False, "error": message}), status


class SignatureAuth:
    """Verifies signed requests for one server. Instance-scoped nonce cache so two
    servers in the same process keep independent replay windows."""

    def __init__(
        self,
        *,
        token: str,
        key: str,
        tolerance_seconds: int = 300,
        max_body_bytes: int = 2048,
        unconfigured_message: str = "auth is not configured",
    ) -> None:
        self._token = token
        self._key = key
        self.tolerance = tolerance_seconds
        self.max_body_bytes = max_body_bytes
        self._unconfigured_message = unconfigured_message
        self._nonces: OrderedDict[str, float] = OrderedDict()
        self._nonces_lock = threading.Lock()

    def configured(self) -> bool:
        return bool(self._token and self._key)

    @staticmethod
    def _body_hash(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()

    def _signature_payload(self, timestamp: str, nonce: str, method: str, path: str, body: bytes) -> bytes:
        msg = "\n".join([timestamp, nonce, method.upper(), path, self._body_hash(body)])
        return msg.encode("utf-8")

    def expected_signature(self, timestamp: str, nonce: str, method: str, path: str, body: bytes) -> str:
        return hmac.new(
            self._key.encode("utf-8"),
            self._signature_payload(timestamp, nonce, method, path, body),
            hashlib.sha256,
        ).hexdigest()

    def _check_nonce(self, nonce: str, now: float) -> bool:
        with self._nonces_lock:
            cutoff = now - self.tolerance
            while self._nonces and next(iter(self._nonces.values())) < cutoff:
                self._nonces.popitem(last=False)
            if nonce in self._nonces:
                return False
            self._nonces[nonce] = now
            return True

    def verify(self) -> tuple[dict[str, Any] | None, Any | None]:
        """Authenticate the current Flask request.

        Returns (payload, None) on success where payload is the parsed JSON object
        (an empty dict for an empty body), or (None, error_response) on failure.
        """
        if not self.configured():
            return None, json_error(503, self._unconfigured_message)

        auth = request.headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(token, self._token):
            return None, json_error(401, "unauthorized")

        ts = request.headers.get("X-Shorterm-Timestamp", "")
        nonce = request.headers.get("X-Shorterm-Nonce", "")
        sig = request.headers.get("X-Shorterm-Signature", "")
        if not ts or not nonce or not sig:
            return None, json_error(401, "missing signature headers")

        try:
            ts_int = int(ts)
        except ValueError:
            return None, json_error(401, "invalid timestamp")

        now = time.time()
        if abs(now - ts_int) > self.tolerance:
            return None, json_error(401, "stale timestamp")
        if not self._check_nonce(nonce, now):
            return None, json_error(401, "replayed nonce")

        body = request.get_data(cache=True) or b""
        if len(body) > self.max_body_bytes:
            return None, json_error(413, "request body too large")

        expected = self.expected_signature(ts, nonce, request.method, request.path, body)
        if not secrets.compare_digest(sig, expected):
            return None, json_error(401, "invalid signature")

        payload: dict[str, Any] = {}
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                return None, json_error(400, "invalid json")
            if not isinstance(payload, dict):
                return None, json_error(400, "json body must be an object")
        return payload, None
