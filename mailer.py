"""SMTP email sending for the auto-responder's email channel.

Stdlib only (smtplib + email). Config comes from the environment:
  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS, FROM_EMAIL.
FROM_EMAIL defaults to SMTP_USER, then FF_USERNAME.

Gmail: SMTP_HOST=smtp.gmail.com, SMTP_PORT=587, and SMTP_PASS must be an
App Password (not your account password).
"""
import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)


def _config() -> dict:
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


def is_configured() -> bool:
    cfg = _config()
    return bool(cfg["host"] and cfg["user"] and cfg["password"] and cfg["from_addr"])


def send_email(to_addr: str, subject: str, body: str) -> None:
    """Send a plain-text email. Raises RuntimeError if SMTP isn't configured."""
    if not to_addr:
        raise RuntimeError("No recipient address for email send.")
    cfg = _config()
    if not is_configured():
        raise RuntimeError(
            "SMTP not configured — set SMTP_HOST, SMTP_USER, SMTP_PASS "
            "(and FROM_EMAIL) in .env to enable the email channel."
        )

    msg = EmailMessage()
    msg["From"] = cfg["from_addr"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as smtp:
        smtp.starttls()
        smtp.login(cfg["user"], cfg["password"])
        smtp.send_message(msg)
    log.info("Email sent to %s (subject=%r)", to_addr, subject)
