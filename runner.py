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
import mailer
import responder
import storage
from notify import notify
from sites import furnishedfinder

log = logging.getLogger(__name__)

OTP_PATH = Path(__file__).parent / "OTP_CODE"


def _channels() -> set[str]:
    """Which reply channels are enabled (REPLY_CHANNELS=platform,email)."""
    raw = os.getenv("REPLY_CHANNELS", "platform,email")
    return {c.strip().lower() for c in raw.split(",") if c.strip()}

_lock = threading.Lock()
_state = {
    "status": "idle",   # idle | launching | checking | waiting_for_otp | done | error
    "message": "",
    "counts": {},
    "running": False,
    "updated_at": None,
}


def _set(**kwargs) -> None:
    with _lock:
        _state.update(kwargs)
        _state["updated_at"] = datetime.now().isoformat(timespec="seconds")


def get_state() -> dict:
    with _lock:
        return dict(_state)


def _status_cb(state: str, message: str = "") -> None:
    # Don't let a terminal callback flip `running` off — the worker owns that.
    _set(status=state, message=message)


def _draft_new_leads(site: str, kind: str, new_items: list[dict]) -> None:
    """Auto-draft replies for newly-seen leads. Runs inside the scrape thread,
    after the browser work — no network send, just stores draft/skipped."""
    if kind != "lead":
        return
    try:
        units = responder.load_units()
    except Exception:
        log.exception("Could not load units; skipping auto-draft")
        return
    for it in new_items:
        try:
            d = responder.evaluate_lead(it, units=units)
            storage.save_response(
                site, kind, it["id"],
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
                site, kind, it["id"], status="skipped",
                reason=f"draft error: {e}",
            )


def _worker() -> None:
    furnishedfinder.STATUS_CB = _status_cb
    try:
        counts = check_leads.run_scrape(
            status_cb=_status_cb, on_new_items=_draft_new_leads
        )
        _set(status="done", message="Done.", counts=counts, running=False)
    except Exception as e:
        log.exception("Scrape failed")
        _set(status="error", message=str(e), running=False)
    finally:
        furnishedfinder.STATUS_CB = None


def start_scrape() -> dict:
    """Kick off a scrape if one isn't already running. Returns current state."""
    with _lock:
        if _state["running"]:
            return dict(_state)
        _state.update(
            status="launching", message="Starting…", counts={}, running=True,
            updated_at=datetime.now().isoformat(timespec="seconds"),
        )
    threading.Thread(target=_worker, daemon=True).start()
    return get_state()


def submit_otp(code: str) -> bool:
    """Hand an OTP code to the running scraper via the ./OTP_CODE file."""
    code = (code or "").strip()
    if not code:
        return False
    OTP_PATH.write_text(code)
    _set(status="checking", message="OTP submitted, verifying…")
    return True


# ---------------------------------------------------------------------------
# One-click send (draft → approve → send)
# ---------------------------------------------------------------------------

_send_lock = threading.Lock()


def _send_worker(site: str, lead: dict, text: str) -> None:
    item_id = lead["id"]
    who = lead.get("traveler") or lead.get("sender") or "tenant"
    channels = _channels()
    # The address the responder extracted at draft time, stored on the response.
    tenant_email = (storage.get_responses(site).get(item_id) or {}).get("tenant_email")

    furnishedfinder.STATUS_CB = _status_cb
    try:
        # 1. Platform reply — the source of truth for status=sent.
        if "platform" in channels:
            _set(status="checking", message=f"Sending platform reply to {who}…")
            with check_leads.browser_page() as page:
                furnishedfinder.send_reply(page, lead, text)
        now = datetime.now().isoformat(timespec="seconds")
        storage.update_response(site, item_id, status="sent", draft=text, sent_at=now)

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
                        tenant_email, "Re: your FurnishedFinder inquiry", text
                    )
                    storage.update_response(
                        site, item_id,
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
        furnishedfinder.STATUS_CB = None
        with _send_lock:
            _state["running"] = False


def send_reply(site: str, lead: dict, text: str) -> dict:
    """Send an approved draft in a background thread. `lead` is the stored
    payload (must include id + traveler/received for row matching)."""
    with _lock:
        if _state["running"]:
            return dict(_state)
        _state.update(
            status="launching", message="Starting…", running=True,
            updated_at=datetime.now().isoformat(timespec="seconds"),
        )
    threading.Thread(
        target=_send_worker, args=(site, lead, text), daemon=True
    ).start()
    return get_state()
