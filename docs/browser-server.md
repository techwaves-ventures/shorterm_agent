# Browser facade server (`browser_server.py`)

An **independent, auth-gated HTTP server** that lets another service drive
FurnishedFinder browser work through Playwright — log in, hand back the login
OTP, collect leads/messages, and send an approved reply — without giving that
caller direct browser access.

It is a *thin facade*: every endpoint delegates to the already-hardened runner
(`runner.py`) and dedup store (`storage.py`). It reuses `runner.py`,
`sites/furnishedfinder.py`, and the DB schema unchanged, and shares the same
Shorterm SQLite/Postgres database and in-process run state. Nothing in this file
talks to Playwright directly.

## Security posture

Mirrors `chrome_task_server.py` via the shared `sig_auth.SignatureAuth`:

- runs on a browser-capable host; bind host is explicit in env
- no `/docs` or OpenAPI surface (plain Flask)
- `GET /healthz` is signed-auth only; unauthenticated callers get `401`
- every request requires:
  - `Authorization: Bearer <BROWSER_SERVER_BEARER_TOKEN>`
  - `X-Shorterm-Timestamp` Unix seconds, within the configured tolerance
  - `X-Shorterm-Nonce`, accepted once per instance window (replay-proof)
  - `X-Shorterm-Signature`, HMAC-SHA256 over
    `timestamp + "\n" + nonce + "\n" + METHOD + "\n" + path + "\n" + sha256(body)`
- fails closed if `BROWSER_SERVER_BEARER_TOKEN` or `BROWSER_SERVER_HMAC_KEY` is
  missing
- request bodies over `BROWSER_SERVER_MAX_BODY_BYTES` (default 16 KiB) are
  rejected with `413`
- **OTP is human-in-the-loop**: `/v1/otp` routes the code the human received to
  the waiting run and is never logged, echoed back, or persisted

## Environment

```bash
BROWSER_SERVER_HOST=127.0.0.1        # 0.0.0.0 to accept off-host callers
BROWSER_SERVER_PORT=6767
BROWSER_SERVER_BEARER_TOKEN=...       # required; fails closed if unset
BROWSER_SERVER_HMAC_KEY=...           # required; fails closed if unset
BROWSER_SERVER_TIMESTAMP_TOLERANCE_SECONDS=300
BROWSER_SERVER_MAX_BODY_BYTES=16384
```

Run it:

```bash
python browser_server.py
```

## Endpoints

All functional endpoints are `POST` with a signed JSON body. `tenant_id` is
optional and defaults to `"1"` (the operator); it is coerced to a string.

| Endpoint | Body | Delegates to | Returns |
| --- | --- | --- | --- |
| `POST /v1/login` | `{tenant_id?}` | `runner.start_scrape` | `{ok, state}` — starts an FF login + scrape run |
| `POST /v1/state` | `{tenant_id?}` | `runner.get_state` | `{ok, state}` — run-state snapshot |
| `POST /v1/otp` | `{tenant_id?, code}` | `runner.submit_otp` | `{ok, state}` — routes the login OTP to the waiting run |
| `POST /v1/leads` | `{tenant_id?, limit?}` | `storage.get_recent` | `{ok, leads}` — collected leads + responder decisions |
| `POST /v1/messages` | `{tenant_id?, limit?}` | `storage.get_recent` | `{ok, messages}` — collected messages + decisions |
| `POST /v1/reply` | `{tenant_id?, item_id, text}` | `runner.send_reply` | `{ok, state}` — send an approved reply to a stored lead/message |
| `GET /healthz` | — | — | `{ok, service}` — authenticated liveness |

`limit` defaults to 20 and is capped at 200.

## Login + OTP flow

1. `POST /v1/login` — the run opens the browser, submits the FF email, and blocks
   at the OTP / magic-link step. Its state moves to `waiting_for_otp`.
2. Poll `POST /v1/state` until `status == "waiting_for_otp"`.
3. A human retrieves the OTP (or login URL) FurnishedFinder sent and hands it
   back with `POST /v1/otp {code}`.
4. The run resumes, verifies the session, collects leads/messages into the shared
   store, and finishes (`status == "done"`).
5. Read results with `POST /v1/leads` and `POST /v1/messages`.
6. `POST /v1/reply` sends an approved (optionally human-edited) reply through the
   same platform path the dashboard uses.

Runs are serialized by the runner's global lock (one browser at a time), and a
tenant can only unblock or read its own run — the same isolation the dashboard
relies on.

## Signing example

```python
import hashlib, hmac, json, time, requests

TOKEN, KEY = "...", "..."
base = "http://127.0.0.1:6767"

def call(path, obj):
    body = json.dumps(obj, separators=(",", ":")).encode()
    ts, nonce = str(int(time.time())), f"n-{time.time_ns()}"
    digest = hashlib.sha256(body).hexdigest()
    msg = "\n".join([ts, nonce, "POST", path, digest]).encode()
    sig = hmac.new(KEY.encode(), msg, hashlib.sha256).hexdigest()
    return requests.post(base + path, data=body, headers={
        "Authorization": f"Bearer {TOKEN}",
        "X-Shorterm-Timestamp": ts,
        "X-Shorterm-Nonce": nonce,
        "X-Shorterm-Signature": sig,
        "Content-Type": "application/json",
    })

call("/v1/login", {"tenant_id": "1"})
```

## Scope

This slice is the additive Option-A facade approved on VEN-41. A fully decoupled,
stateless browser service (its own process/DB, no shared in-process run state) is
tracked as a follow-up, to be taken on only after this slice is reviewed.
