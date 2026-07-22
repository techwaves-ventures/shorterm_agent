"""Focused tests for the browser facade server.

Cover the reused auth surface (unauth/replay/body-size) and that each fixed
endpoint delegates to the runner/storage seams with the right tenant — without
ever launching Playwright or sending a live reply (runner/storage are patched).
"""
import hashlib
import hmac
import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from cryptography.fernet import Fernet

_TMP = tempfile.mkdtemp(prefix="browser_server_test_")
os.environ["SQLITE_PATH"] = str(Path(_TMP) / "test.db")
os.environ.pop("DATABASE_URL", None)
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["FF_CRED_KEY"] = Fernet.generate_key().decode()
os.environ["BROWSER_SERVER_BEARER_TOKEN"] = "test-bearer-token"
os.environ["BROWSER_SERVER_HMAC_KEY"] = "test-hmac-key"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

browser_server = importlib.import_module("browser_server")


def _signed_headers(path, method="POST", body=b"", nonce="nonce-1"):
    ts = str(int(time.time()))
    digest = hashlib.sha256(body).hexdigest()
    msg = "\n".join([ts, nonce, method, path, digest]).encode()
    sig = hmac.new(b"test-hmac-key", msg, hashlib.sha256).hexdigest()
    return {
        "Authorization": "Bearer test-bearer-token",
        "X-Shorterm-Timestamp": ts,
        "X-Shorterm-Nonce": nonce,
        "X-Shorterm-Signature": sig,
        "Content-Type": "application/json",
    }


def _post(client, path, obj, nonce):
    body = json.dumps(obj, separators=(",", ":")).encode()
    return client.post(path, data=body, headers=_signed_headers(path, body=body, nonce=nonce))


# --- auth surface (reused from sig_auth) ------------------------------------


def test_health_requires_auth_and_no_docs():
    client = browser_server.app.test_client()
    assert client.get("/healthz").status_code == 401
    ok = client.get("/healthz", headers=_signed_headers("/healthz", method="GET", nonce="h1"))
    assert ok.status_code == 200
    assert ok.get_json()["service"] == "shorterm-browser-server"
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    # Wrong method on a POST-only route is a JSON 405, not an HTML page.
    res = client.get("/v1/leads")
    assert res.status_code == 405
    assert res.is_json


def test_endpoints_reject_unauthenticated():
    client = browser_server.app.test_client()
    for path in ("/v1/login", "/v1/state", "/v1/otp", "/v1/leads", "/v1/messages", "/v1/reply"):
        assert client.post(path, json={}).status_code == 401


def test_bad_signature_rejected():
    client = browser_server.app.test_client()
    body = b"{}"
    headers = _signed_headers("/v1/state", body=body, nonce="bad-sig")
    headers["X-Shorterm-Signature"] = "0" * 64
    assert client.post("/v1/state", data=body, headers=headers).status_code == 401


def test_replayed_nonce_rejected(monkeypatch):
    monkeypatch.setattr(browser_server.runner, "get_state", lambda tid=None: {"status": "idle"})
    client = browser_server.app.test_client()
    body = b"{}"
    headers = _signed_headers("/v1/state", body=body, nonce="replay-1")
    assert client.post("/v1/state", data=body, headers=headers).status_code == 200
    assert client.post("/v1/state", data=body, headers=headers).status_code == 401


def test_oversized_body_rejected():
    client = browser_server.app.test_client()
    big = ("x" * (browser_server.MAX_BODY_BYTES + 1)).encode()
    res = client.post("/v1/reply", data=big, headers=_signed_headers("/v1/reply", body=big, nonce="big"))
    assert res.status_code == 413


# --- endpoint delegation ----------------------------------------------------


def test_login_starts_scrape_for_tenant(monkeypatch):
    seen = {}

    def fake_start(tid):
        seen["tid"] = tid
        return {"status": "launching"}

    monkeypatch.setattr(browser_server.runner, "start_scrape", fake_start)
    client = browser_server.app.test_client()
    res = _post(client, "/v1/login", {"tenant_id": 7}, "login-1")
    assert res.status_code == 200
    assert res.get_json()["state"]["status"] == "launching"
    assert seen["tid"] == "7"  # coerced to str


def test_state_defaults_to_operator_tenant(monkeypatch):
    seen = {}
    monkeypatch.setattr(browser_server.runner, "get_state",
                        lambda tid=None: seen.setdefault("tid", tid) or {"status": "idle"})
    client = browser_server.app.test_client()
    res = _post(client, "/v1/state", {}, "state-1")
    assert res.status_code == 200
    assert seen["tid"] == "1"


def test_otp_routes_code_without_persisting(monkeypatch):
    captured = {}

    def fake_submit(tid, code):
        captured["tid"], captured["code"] = tid, code
        return True

    monkeypatch.setattr(browser_server.runner, "submit_otp", fake_submit)
    monkeypatch.setattr(browser_server.runner, "get_state", lambda tid=None: {"status": "checking"})
    client = browser_server.app.test_client()
    res = _post(client, "/v1/otp", {"tenant_id": "3", "code": "123456"}, "otp-1")
    assert res.status_code == 200
    assert res.get_json()["ok"] is True
    assert captured == {"tid": "3", "code": "123456"}
    # The code is handed to the runner but never echoed back in the response.
    assert "123456" not in res.get_data(as_text=True)


def test_otp_rejects_non_string_code(monkeypatch):
    monkeypatch.setattr(browser_server.runner, "submit_otp", lambda *a: True)
    monkeypatch.setattr(browser_server.runner, "get_state", lambda tid=None: {})
    client = browser_server.app.test_client()
    res = _post(client, "/v1/otp", {"code": 123456}, "otp-bad")
    assert res.status_code == 400


def test_leads_and_messages_collect(monkeypatch):
    calls = []

    def fake_recent(tenant_id, site, kind, limit):
        calls.append((tenant_id, site, kind, limit))
        return [{"id": f"{kind}-1"}]

    monkeypatch.setattr(browser_server.storage, "get_recent", fake_recent)
    monkeypatch.setattr(browser_server.storage, "get_responses",
                        lambda tid, site: {f"{'lead'}-1": {"status": "draft"}})
    client = browser_server.app.test_client()

    leads = _post(client, "/v1/leads", {"tenant_id": "2", "limit": 5}, "leads-1")
    assert leads.status_code == 200
    body = leads.get_json()
    assert body["leads"][0]["kind"] == "lead"
    assert body["leads"][0]["response"] == {"status": "draft"}
    assert calls[-1] == ("2", "furnishedfinder", "lead", 5)

    msgs = _post(client, "/v1/messages", {"tenant_id": "2"}, "msgs-1")
    assert msgs.status_code == 200
    assert msgs.get_json()["messages"][0]["kind"] == "message"
    assert calls[-1] == ("2", "furnishedfinder", "message", 20)  # default limit


def test_limit_is_capped(monkeypatch):
    calls = []
    monkeypatch.setattr(browser_server.storage, "get_recent",
                        lambda t, s, k, limit: calls.append(limit) or [])
    monkeypatch.setattr(browser_server.storage, "get_responses", lambda t, s: {})
    client = browser_server.app.test_client()
    _post(client, "/v1/leads", {"limit": 9999}, "cap-1")
    assert calls[-1] == browser_server.MAX_LIMIT


def test_reply_sends_located_item(monkeypatch):
    sent = {}

    def fake_recent(tenant_id, site, kind, limit):
        return [{"id": "lead-9", "traveler": "Sam"}] if kind == "lead" else []

    def fake_send(tenant_id, site, item, text):
        sent["item"], sent["text"], sent["tid"] = item, text, tenant_id
        return {"status": "launching"}

    monkeypatch.setattr(browser_server.storage, "get_recent", fake_recent)
    monkeypatch.setattr(browser_server.runner, "send_reply", fake_send)
    client = browser_server.app.test_client()
    res = _post(client, "/v1/reply", {"item_id": "lead-9", "text": "Hi Sam"}, "reply-1")
    assert res.status_code == 200
    assert sent["tid"] == "1"
    assert sent["item"]["kind"] == "lead"
    assert sent["text"] == "Hi Sam"


def test_reply_missing_item_is_404(monkeypatch):
    monkeypatch.setattr(browser_server.storage, "get_recent", lambda *a: [])
    called = {"sent": False}
    monkeypatch.setattr(browser_server.runner, "send_reply",
                        lambda *a: called.__setitem__("sent", True))
    client = browser_server.app.test_client()
    res = _post(client, "/v1/reply", {"item_id": "nope", "text": "hi"}, "reply-404")
    assert res.status_code == 404
    assert called["sent"] is False  # no send attempted for a missing item


def test_reply_requires_item_id_and_text(monkeypatch):
    monkeypatch.setattr(browser_server.runner, "send_reply", lambda *a: {})
    monkeypatch.setattr(browser_server.storage, "get_recent", lambda *a: [{"id": "x"}])
    client = browser_server.app.test_client()
    assert _post(client, "/v1/reply", {"text": "hi"}, "r-no-id").status_code == 400
    assert _post(client, "/v1/reply", {"item_id": "x", "text": "  "}, "r-empty").status_code == 400
