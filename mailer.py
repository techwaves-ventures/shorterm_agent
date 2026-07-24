"""Outbound email — Resend (preferred) or plain SMTP.

Three kinds of mail leave this app, with very different deliverability stakes:

  * **Guest replies** — the email channel of an approved reply. These go to
    someone who has never heard of us, so they must authenticate cleanly or they
    land in spam and the host loses the booking.
  * **Daily digest** — to the host's own inbox.
  * **Password resets** — to the host's own inbox, and must actually arrive.

Provider is auto-selected: set `RESEND_API_KEY` to use Resend, otherwise the
SMTP vars are used, otherwise email is disabled and callers degrade gracefully.

## Why the From address is not the host's address

The old behaviour set `From:` to the host's own address (e.g. their Gmail) while
relaying through *our* server. That fails SPF/DKIM — we are not authorised to
send as gmail.com — so it gets spam-foldered or rejected outright, and Resend
refuses it entirely (you may only send from a domain you have verified).

So we send from our own verified sender and carry the host's identity properly:

    From:     Sagiv <hello@yourdomain.com>     <- verified, authenticates
    Reply-To: sagiv@gmail.com                  <- replies reach the host

The guest sees the host's name, and replying reaches the host directly. This is
the standard pattern for sending "on behalf of" a user, and the only one that
both authenticates and doesn't spoof.
"""
import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

import requests

log = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"
RESEND = "resend"
SMTP = "smtp"
NONE = "none"


def _resend_key() -> str:
    return (os.getenv("RESEND_API_KEY") or "").strip()


def _resend_from() -> str:
    """Our verified sender, e.g. "Shorterm <hello@yourdomain.com>" or a bare address."""
    return (os.getenv("RESEND_FROM") or os.getenv("FROM_EMAIL") or "").strip()


def _smtp_config() -> dict:
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASS", "")
    from_addr = (
        os.getenv("FROM_EMAIL", "").strip()
        or user
        or os.getenv("FF_USERNAME", "").strip()
    )
    return {
        "host": host,
        "port": int(os.getenv("SMTP_PORT", "587")),
        "user": user,
        "password": password,
        "from_addr": from_addr,
    }


def provider() -> str:
    """Which backend will actually send: 'resend', 'smtp', or 'none'."""
    if _resend_key() and _resend_from():
        return RESEND
    cfg = _smtp_config()
    if cfg["host"] and cfg["user"] and cfg["password"] and cfg["from_addr"]:
        return SMTP
    return NONE


def is_configured() -> bool:
    return provider() != NONE


def _display_name(from_email: str, from_name: str) -> str:
    """The name a recipient sees. Prefer the host's name, fall back to their
    address's local part, so it never reads as coming from a faceless system."""
    if from_name.strip():
        return from_name.strip()
    local = parseaddr(from_email)[1].partition("@")[0]
    return local.replace(".", " ").title() if local else ""


def _sender(from_email: str, from_name: str) -> tuple[str, str | None]:
    """(From header, Reply-To) for the configured provider.

    `from_email` is who the message is *really* from (the host). We never put it
    in `From:` — see the module docstring — but we do surface their name and
    route replies back to them.
    """
    kind = provider()
    base = _resend_from() if kind == RESEND else _smtp_config()["from_addr"]
    name, addr = parseaddr(base)
    addr = addr or base
    host_addr = parseaddr(from_email)[1] if from_email else ""

    if not host_addr:
        # System mail (password reset, digest to the account owner).
        return (formataddr((name, addr)) if name else addr), None

    label = _display_name(from_email, from_name)
    if name and label:
        label = f"{label} via {name}"
    return formataddr((label or name, addr)), host_addr


def _send_resend(to_addr: str, subject: str, body: str,
                 from_header: str, reply_to: str | None) -> None:
    payload = {
        "from": from_header,
        "to": [to_addr],
        "subject": subject,
        "text": body,
    }
    if reply_to:
        payload["reply_to"] = [reply_to]
    resp = requests.post(
        RESEND_ENDPOINT,
        json=payload,
        headers={
            "Authorization": f"Bearer {_resend_key()}",
            "Content-Type": "application/json",
        },
        timeout=20,
    )
    if resp.status_code >= 300:
        # Surface Resend's own message — it names the actual problem (usually an
        # unverified sending domain), which is what the operator needs to fix.
        detail = ""
        try:
            detail = (resp.json() or {}).get("message") or resp.text[:200]
        except ValueError:
            detail = resp.text[:200]
        raise RuntimeError(f"Resend rejected the message ({resp.status_code}): {detail}")


def _send_smtp(to_addr: str, subject: str, body: str,
               from_header: str, reply_to: str | None) -> None:
    cfg = _smtp_config()
    msg = EmailMessage()
    msg["From"] = from_header
    msg["To"] = to_addr
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as smtp:
        smtp.starttls()
        smtp.login(cfg["user"], cfg["password"])
        smtp.send_message(msg)


def send_email(to_addr: str, subject: str, body: str, from_email: str = "",
               from_name: str = "") -> None:
    """Send a plain-text email via the configured provider.

    `from_email` is the host this message is on behalf of: it becomes the
    Reply-To and supplies the display name, never the authenticated From.
    Raises RuntimeError when email isn't configured or the provider rejects it.
    """
    if not to_addr:
        raise RuntimeError("No recipient address for email send.")
    kind = provider()
    if kind == NONE:
        raise RuntimeError(
            "Email isn't configured — set RESEND_API_KEY + RESEND_FROM (preferred), "
            "or SMTP_HOST/SMTP_USER/SMTP_PASS, in .env."
        )
    from_header, reply_to = _sender(from_email, from_name)
    if kind == RESEND:
        _send_resend(to_addr, subject, body, from_header, reply_to)
    else:
        _send_smtp(to_addr, subject, body, from_header, reply_to)
    log.info("Email sent via %s to %s (subject=%r)", kind, to_addr, subject)
