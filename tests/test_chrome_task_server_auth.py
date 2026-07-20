import hashlib
import hmac
import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path


_TMP = tempfile.mkdtemp(prefix="chrome_task_server_test_")
os.environ["SQLITE_PATH"] = str(Path(_TMP) / "test.db")
os.environ.pop("DATABASE_URL", None)
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["FF_CRED_KEY"] = "a" * 44
os.environ["CHROME_TASK_BEARER_TOKEN"] = "test-bearer-token"
os.environ["CHROME_TASK_HMAC_KEY"] = "test-hmac-key"
os.environ["CHROME_TASK_MAX_JOBS_PER_WAKE"] = "2"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

chrome_task_server = importlib.import_module("chrome_task_server")


def _signed_headers(path="/v1/wake", body=b'{"max_jobs":1}', nonce="nonce-1"):
    ts = str(int(time.time()))
    digest = hashlib.sha256(body).hexdigest()
    msg = "\n".join([ts, nonce, "POST", path, digest]).encode()
    sig = hmac.new(b"test-hmac-key", msg, hashlib.sha256).hexdigest()
    return {
        "Authorization": "Bearer test-bearer-token",
        "X-Shorterm-Timestamp": ts,
        "X-Shorterm-Nonce": nonce,
        "X-Shorterm-Signature": sig,
        "Content-Type": "application/json",
    }


def test_health_and_no_docs():
    client = chrome_task_server.app.test_client()
    assert client.get("/healthz").status_code == 200
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_wake_requires_auth():
    client = chrome_task_server.app.test_client()
    assert client.post("/v1/wake", json={"max_jobs": 1}).status_code == 401


def test_wake_rejects_bad_signature():
    client = chrome_task_server.app.test_client()
    body = b'{"max_jobs":1}'
    headers = _signed_headers(body=body, nonce="bad-sig")
    headers["X-Shorterm-Signature"] = "0" * 64
    assert client.post("/v1/wake", data=body, headers=headers).status_code == 401


def test_wake_claims_existing_jobs_only(monkeypatch):
    calls = []

    def fake_run_once(worker_id):
        calls.append(worker_id)
        return len(calls) == 1

    monkeypatch.setattr(chrome_task_server.worker, "run_once", fake_run_once)
    monkeypatch.setattr(chrome_task_server.jobs, "worker_online", lambda: True)

    client = chrome_task_server.app.test_client()
    body = json.dumps({"max_jobs": 2}, separators=(",", ":")).encode()
    res = client.post("/v1/wake", data=body, headers=_signed_headers(body=body, nonce="nonce-claim"))
    assert res.status_code == 200
    assert res.get_json()["claimed"] == 1
    assert len(calls) == 2


def test_wake_rejects_replayed_nonce(monkeypatch):
    monkeypatch.setattr(chrome_task_server.worker, "run_once", lambda _worker_id: False)
    monkeypatch.setattr(chrome_task_server.jobs, "worker_online", lambda: True)

    client = chrome_task_server.app.test_client()
    body = b'{"max_jobs":1}'
    headers = _signed_headers(body=body, nonce="nonce-replay")
    assert client.post("/v1/wake", data=body, headers=headers).status_code == 200
    assert client.post("/v1/wake", data=body, headers=headers).status_code == 401
