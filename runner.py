"""Background scrape orchestration for the dashboard.

Runs the existing `check_leads.run_scrape` in a daemon thread so the Flask
request handler returns immediately, exposes a thread-safe state snapshot for
the UI to poll, and bridges OTP entry from the browser to the scraper (which
polls the ./OTP_CODE file).
"""
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

import check_leads
import config
import ff_account
import mailer
import responder
import storage
from notify import notify
from sites import furnishedfinder

log = logging.getLogger(__name__)

OTP_PATH = Path(__file__).parent / "OTP_CODE"
_OTP_TIMEOUT = 600  # seconds a run waits for the tenant to submit their OTP


def _channels(tenant_id: str) -> set[str]:
    """Which reply channels are enabled for this tenant (e.g. platform,email)."""
    raw = config.get_settings(tenant_id)["reply_channels"]
    return {c.strip().lower() for c in raw.split(",") if c.strip()}


def _ff_username(tenant_id: str) -> str:
    """The FF email to log in as: the operator uses FF_USERNAME env; other
    tenants use their connected (decrypted) account."""
    if str(tenant_id) == "1":
        return os.getenv("FF_USERNAME", "")
    return ff_account.get_username(tenant_id) or ""


_lock = threading.Lock()
_state = {
    "status": "idle",   # idle | launching | checking | waiting_for_otp | done | error
    "message": "",
    "counts": {},
    "running": False,
    "tenant_id": None,  # which tenant the current/last run belongs to
    "updated_at": None,
}

# Per-tenant OTP rendezvous: a running scrape registers a waiter, then blocks
# until the matching tenant submits a code on their own dashboard. Keyed by
# tenant_id so one tenant can never unblock another's run.
_otp_lock = threading.Lock()
_otp_waiters: dict[str, dict] = {}


def _set(**kwargs) -> None:
    with _lock:
        _state.update(kwargs)
        _state["updated_at"] = datetime.now().isoformat(timespec="seconds")


def get_state(tenant_id: str | None = None) -> dict:
    """Snapshot of the run state. If `tenant_id` is given and a run is active for
    a *different* tenant, return an idle snapshot — status messages can contain
    another tenant's traveler names, so they must not leak across tenants."""
    with _lock:
        snap = dict(_state)
    if tenant_id is not None and snap.get("tenant_id") not in (None, str(tenant_id)):
        return {
            "status": "idle", "message": "", "counts": {}, "running": False,
            "tenant_id": None, "updated_at": snap.get("updated_at"),
        }
    return snap


def _otp_provider(tenant_id: str):
    """Return a callable that blocks until this tenant submits an OTP (or times
    out), then returns the code. Registered as the FF context's otp_provider."""
    tid = str(tenant_id)
    ev = threading.Event()
    with _otp_lock:
        _otp_waiters[tid] = {"event": ev, "code": None}

    def provider() -> str | None:
        got = ev.wait(timeout=_OTP_TIMEOUT)
        with _otp_lock:
            entry = _otp_waiters.pop(tid, None)
        return entry["code"] if (got and entry) else None

    return provider


def _status_cb(state: str, message: str = "") -> None:
    # Don't let a terminal callback flip `running` off — the worker owns that.
    _set(status=state, message=message)


def _draft_new_items(tenant_id: str, site: str, kind: str, new_items: list[dict]) -> None:
    """Auto-draft replies for newly-seen leads AND messages. Runs inside the
    scrape thread, after the browser work — no network send, just stores
    draft/skipped. Leads get an introduction; messages get a response (the
    responder branches on item['kind'])."""
    try:
        units = responder.load_units(tenant_id)
    except Exception:
        log.exception("Could not load units; skipping auto-draft")
        return
    for it in new_items:
        it.setdefault("kind", kind)  # ensure the responder sees the right mode
        try:
            d = responder.evaluate_lead(it, tenant_id, units=units)
            storage.save_response(
                tenant_id, site, kind, it["id"],
                status="draft" if d.get("fit") else "skipped",
                unit_id=d.get("unit_id"),
                reason=d.get("reason"),
                draft=d.get("draft"),
                confidence=d.get("confidence"),
                tenant_email=d.get("tenant_email"),
            )
        except Exception as e:
            log.exception("Auto-draft failed for %s", it.get("id"))
            storage.save_response(
                tenant_id, site, kind, it["id"], status="skipped",
                reason=f"draft error: {e}",
            )


def _mark_ff_state(tenant_id: str, state: str, error: str | None = None) -> None:
    """Mirror the run outcome into the tenant's FF account state so a connection
    only reads as `connected` after a real scrape/login succeeds. No-op for the
    operator (tenant '1'), which has no ff_accounts row."""
    if str(tenant_id) == "1":
        return
    try:
        ff_account.mark_state(tenant_id, state, error=error)
    except Exception:
        log.exception("Could not update FF account state to %s", state)


def _worker(tenant_id: str) -> None:
    username = _ff_username(tenant_id)
    furnishedfinder.set_context(username, _otp_provider(tenant_id), _status_cb)
    _mark_ff_state(tenant_id, ff_account.VERIFYING)
    try:
        counts = check_leads.run_scrape(
            status_cb=_status_cb, on_new_items=_draft_new_items, tenant_id=tenant_id
        )
        # A completed scrape means a real FF session was established.
        _mark_ff_state(tenant_id, ff_account.CONNECTED)
        _set(status="done", message="Done.", counts=counts, running=False)
    except Exception as e:
        log.exception("Scrape failed")
        _mark_ff_state(tenant_id, ff_account.ERROR,
                       error="Couldn't verify your FurnishedFinder login. Please try Check now again.")
        _set(status="error", message=str(e), running=False)
    finally:
        furnishedfinder.clear_context()


def start_scrape(tenant_id: str = "1") -> dict:
    """Kick off a scrape if one isn't already running. Returns current state.

    Runs are serialized by the global run-lock (one browser at a time); the
    tenant's FF account + isolated profile are bound for the duration."""
    tenant_id = str(tenant_id)
    with _lock:
        if _state["running"]:
            return dict(_state)
        _state.update(
            status="launching", message="Starting…", counts={}, running=True,
            tenant_id=tenant_id,
            updated_at=datetime.now().isoformat(timespec="seconds"),
        )
    threading.Thread(target=_worker, args=(tenant_id,), daemon=True).start()
    return get_state(tenant_id)


def submit_otp(tenant_id: str, code: str) -> bool:
    """Route an OTP code to this tenant's waiting run. Returns False if there is
    no run waiting for that tenant — so a tenant can only unblock their own run.

    Falls back to the ./OTP_CODE file when no in-process waiter exists (the
    operator's CLI/file flow), preserving the original behavior."""
    code = (code or "").strip()
    if not code:
        return False
    tid = str(tenant_id)
    with _otp_lock:
        entry = _otp_waiters.get(tid)
        if entry is not None:
            entry["code"] = code
            entry["event"].set()
            routed = True
        else:
            routed = False
    if not routed:
        # No in-process waiter. The only path that polls the ./OTP_CODE file is
        # the operator's (tenant '1') CLI/file login; every other tenant's run
        # uses an in-process waiter, so a missing waiter there means "not for
        # you" — reject rather than writing a file they'd never read.
        if tid != "1":
            return False
        OTP_PATH.write_text(code)
    _set(status="checking", message="OTP submitted, verifying…")
    return True


# ---------------------------------------------------------------------------
# One-click send (draft → approve → send)
# ---------------------------------------------------------------------------

_send_lock = threading.Lock()


def _send_worker(tenant_id: str, site: str, item: dict, text: str) -> None:
    item_id = item["id"]
    kind = item.get("kind", "lead")
    who = item.get("traveler") or item.get("sender") or "tenant"
    channels = _channels(tenant_id)
    from_email = config.get_settings(tenant_id)["from_email"]
    # The address the responder extracted at draft time, stored on the response.
    tenant_email = (storage.get_responses(tenant_id, site).get(item_id) or {}).get("tenant_email")

    furnishedfinder.set_context(_ff_username(tenant_id), _otp_provider(tenant_id), _status_cb)
    try:
        # 1. Platform reply — the source of truth for status=sent. Leads use the
        #    "Reply To Tenant" row action; messages reply inside the thread.
        if "platform" in channels:
            _set(status="checking", message=f"Sending platform reply to {who}…")
            with check_leads.browser_page(tenant_id) as page:
                if kind == "message":
                    furnishedfinder.send_message_reply(page, item, text)
                else:
                    furnishedfinder.send_reply(page, item, text)
        now = datetime.now().isoformat(timespec="seconds")
        storage.update_response(tenant_id, site, item_id, status="sent", draft=text, sent_at=now)

        # 2. Email — best-effort; never fails the send if the platform reply went.
        email_note = ""
        if "email" in channels:
            if not tenant_email:
                email_note = " (no email on file — platform only)"
            elif not mailer.is_configured():
                email_note = " (SMTP not configured — platform only)"
            else:
                try:
                    _set(status="checking", message=f"Emailing {tenant_email}…")
                    mailer.send_email(
                        tenant_email, "Re: your FurnishedFinder inquiry", text,
                        from_email=from_email,
                    )
                    storage.update_response(
                        tenant_id, site, item_id,
                        emailed_at=datetime.now().isoformat(timespec="seconds"),
                    )
                    email_note = f" + emailed {tenant_email}"
                except Exception as e:
                    log.exception("Email send failed for %s", item_id)
                    notify(
                        "Email reply failed",
                        f"Platform reply to {who} sent, but email to "
                        f"{tenant_email} failed: {e}",
                    )
                    email_note = f" (email to {tenant_email} FAILED — see logs)"

        _set(status="done", message=f"Reply sent to {who}{email_note}.", running=False)
    except Exception as e:
        log.exception("Send failed for %s", item_id)
        _set(status="error", message=f"Send failed: {e}", running=False)
    finally:
        furnishedfinder.clear_context()
        with _send_lock:
            _state["running"] = False


def send_reply(tenant_id: str, site: str, item: dict, text: str) -> dict:
    """Send an approved draft in a background thread. `item` is the stored
    payload (must include id + kind; traveler/received for leads or
    sender/date for messages, used to locate the thread/row)."""
    tenant_id = str(tenant_id)
    with _lock:
        if _state["running"]:
            return dict(_state)
        _state.update(
            status="launching", message="Starting…", running=True,
            tenant_id=tenant_id,
            updated_at=datetime.now().isoformat(timespec="seconds"),
        )
    threading.Thread(
        target=_send_worker, args=(tenant_id, site, item, text), daemon=True
    ).start()
    return get_state(tenant_id)
