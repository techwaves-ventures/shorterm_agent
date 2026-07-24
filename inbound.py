"""Inbound email ingestion — leads arrive without touching FurnishedFinder.

FurnishedFinder emails the landlord whenever a lead or message arrives. Ingesting
*that* email means we never drive a browser against their site to read anything:
no scheduled scraping, no bot-detection surface, no circumvention. The host adds
one forwarding rule and the pipeline downstream (dedup → deal → draft) is
unchanged.

## The security model

This endpoint is a public, unauthenticated ingress — email is trivially
spoofable, and a fake "lead" injected into a tenant's account would be drafted
at and possibly auto-replied to. Four independent checks, all required:

  1. **Unguessable per-tenant address.** `leads+7-a1b2c3d4e5f6@…` where the
     suffix is an HMAC of the tenant id under SECRET_KEY. Knowing one tenant's
     address tells you nothing about another's, and the tenant id alone is not
     enough to forge one.
  2. **Provider webhook authentication.** The mail provider's shared secret must
     match before the body is looked at (see `verify_webhook`).
  3. **Sender allowlist.** The forwarded message must actually originate from a
     FurnishedFinder domain, so a stranger emailing the address directly is
     rejected even if they somehow guessed it.
  4. **Size cap.** Oversized payloads are dropped before parsing.

Failing any check drops the message and logs it. We never partially trust a
message: a lead we can't attribute confidently is worth less than the damage
from acting on a forged one.
"""
import hashlib
import hmac
import logging
import os
import re

log = logging.getLogger(__name__)

# Domains a genuine FurnishedFinder notification can come from. Checked against
# the *original* sender the forwarding provider reports, not the envelope of the
# forward itself (which is the host's own mailbox).
ALLOWED_SENDER_DOMAINS = (
    "furnishedfinder.com",
    "mail.furnishedfinder.com",
    "email.furnishedfinder.com",
    "notifications.furnishedfinder.com",
)

MAX_PAYLOAD_BYTES = 512 * 1024
_TOKEN_LEN = 16


def inbound_domain() -> str:
    """Domain that receives forwarded lead mail (e.g. inbound.yourdomain.com)."""
    return (os.getenv("INBOUND_EMAIL_DOMAIN") or "").strip().lower()


def configured() -> bool:
    return bool(inbound_domain() and _secret())


def _secret() -> str:
    return (os.getenv("SECRET_KEY") or "").strip()


def _token(tenant_id: str) -> str:
    """Unguessable per-tenant suffix. Derived, so nothing extra to store."""
    return hmac.new(
        _secret().encode(), f"inbound:{tenant_id}".encode(), hashlib.sha256
    ).hexdigest()[:_TOKEN_LEN]


def address_for(tenant_id: str) -> str:
    """The address this tenant forwards their FurnishedFinder mail to."""
    domain = inbound_domain()
    if not domain or not _secret():
        return ""
    return f"leads+{tenant_id}-{_token(tenant_id)}@{domain}"


def tenant_for_address(address: str) -> str | None:
    """Resolve a delivery address back to its tenant, or None if it doesn't verify.

    Compared in constant time so the token can't be recovered by timing.
    """
    if not address or not _secret():
        return None
    local = address.split("@")[0].strip().lower()
    m = re.match(r"^leads\+([0-9a-z_-]+)-([0-9a-f]{%d})$" % _TOKEN_LEN, local)
    if not m:
        return None
    tenant_id, supplied = m.group(1), m.group(2)
    if not hmac.compare_digest(supplied, _token(tenant_id)):
        log.warning("Inbound address failed verification for tenant %s", tenant_id)
        return None
    return tenant_id


def verify_webhook(supplied_secret: str) -> bool:
    """Authenticate the mail provider itself. Fails closed when unconfigured."""
    expected = (os.getenv("INBOUND_WEBHOOK_SECRET") or "").strip()
    if not expected:
        log.error("INBOUND_WEBHOOK_SECRET is not set — refusing inbound mail.")
        return False
    return hmac.compare_digest((supplied_secret or "").strip(), expected)


def sender_allowed(sender: str) -> bool:
    """Whether the original sender is a FurnishedFinder address."""
    addr = (sender or "").strip().lower()
    m = re.search(r"[\w.+-]+@([\w.-]+)", addr)
    if not m:
        return False
    domain = m.group(1)
    return any(domain == d or domain.endswith("." + d) for d in ALLOWED_SENDER_DOMAINS)


def extract_recipient(payload: dict) -> str:
    """The delivery address, across the shapes different providers post.

    Providers disagree on the field name, and a forwarded message's `to:` is
    often the host's own mailbox rather than ours — so the provider-supplied
    envelope recipient is preferred over anything in the headers.
    """
    for key in ("recipient", "to", "envelope_to", "original_recipient", "OriginalRecipient"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, str):
                return first
            if isinstance(first, dict):
                return first.get("email") or first.get("address") or ""
    envelope = payload.get("envelope")
    if isinstance(envelope, dict):
        return envelope.get("to") or envelope.get("recipient") or ""
    return ""


def extract_sender(payload: dict) -> str:
    for key in ("from", "sender", "From", "envelope_from", "reply_to"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            return value.get("email") or value.get("address") or ""
    headers = payload.get("headers")
    if isinstance(headers, dict):
        # A forward wraps the original; these headers preserve who really sent it.
        for key in ("X-Forwarded-For", "X-Original-From", "Reply-To", "From"):
            if headers.get(key):
                return str(headers[key])
    return ""


def extract_body(payload: dict) -> str:
    for key in ("text", "plain", "TextBody", "body-plain", "stripped-text", "body"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    # Fall back to HTML with tags stripped — never ideal, but better than
    # dropping a real lead because the provider sent no plain part.
    for key in ("html", "HtmlBody", "body-html"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            text = re.sub(r"(?is)<(script|style).*?</\1>", " ", value)
            text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
            text = re.sub(r"<[^>]+>", " ", text)
            return re.sub(r"[ \t]+", " ", text)
    return ""


def extract_subject(payload: dict) -> str:
    for key in ("subject", "Subject"):
        if isinstance(payload.get(key), str):
            return payload[key]
    return ""


class Rejected(Exception):
    """Inbound message failed a check. The reason is for logs, never the caller."""


def accept(payload: dict, webhook_secret: str, raw_size: int = 0) -> tuple[str, dict]:
    """Validate an inbound message and return (tenant_id, parsed item).

    Raises `Rejected` on any failure. The caller returns a flat 202 either way,
    so a probe learns nothing about which tenants or addresses exist.
    """
    if not configured():
        raise Rejected("inbound email is not configured")
    if raw_size and raw_size > MAX_PAYLOAD_BYTES:
        raise Rejected("payload too large")
    if not verify_webhook(webhook_secret):
        raise Rejected("bad webhook secret")

    tenant_id = tenant_for_address(extract_recipient(payload))
    if not tenant_id:
        raise Rejected("unrecognised recipient")

    sender = extract_sender(payload)
    if not sender_allowed(sender):
        raise Rejected(f"sender not allowed: {sender[:80]!r}")

    from sites import ff_email

    item = ff_email.parse(extract_subject(payload), extract_body(payload))
    if not item:
        raise Rejected("could not parse a lead from the message")
    return tenant_id, item


def store(tenant_id: str, item: dict, site: str = "furnishedfinder") -> bool:
    """Put an ingested item through the normal pipeline. True if it was new.

    Deliberately the same path a scrape uses — dedup, deal creation and drafting
    behave identically no matter how the lead arrived.
    """
    import config
    import pipeline
    import storage

    kind = item.get("kind", "lead")
    new_items = storage.filter_new(tenant_id, site, kind, [item])
    if not new_items:
        return False
    try:
        pipeline.ensure(tenant_id, site, item, None, units=config.get_units(tenant_id))
    except Exception:
        log.exception("Could not open a deal for ingested item %s", item.get("id"))
    return True
