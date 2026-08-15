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
from email.utils import parseaddr
from html.parser import HTMLParser

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
    """Whether the original sender is a FurnishedFinder address.

    Parsed with `parseaddr` rather than searched with a regex. A regex takes the
    first address-shaped run of characters *anywhere* in the header, and the
    display name comes first — so

        "no-reply@furnishedfinder.com" <attacker@evil.com>

    matched on the quoted display name and was accepted, while the mail actually
    came from the attacker. The address that matters is the one in the angle
    brackets, which is the one `parseaddr` returns.
    """
    _, addr = parseaddr((sender or "").strip())
    addr = addr.strip().lower()
    # parseaddr yields '' for junk, and a bare 'a@b@c' must not be split into a
    # trusted tail — so require exactly one '@' and a non-empty local part.
    if addr.count("@") != 1:
        return False
    local, _, domain = addr.partition("@")
    if not local or not domain:
        return False
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
    """Who sent this, for the allowlist check in `sender_allowed`.

    Ordered by how hard the field is to forge: the provider's envelope sender
    first (it comes from the SMTP transaction, not the message body), then the
    `From` header.

    `Reply-To` and `X-Forwarded-For` are deliberately *not* consulted. Reply-To
    says where a reply should go, not where the mail came from, and an attacker
    sending from anywhere can set it to a FurnishedFinder address to clear the
    allowlist; X-Forwarded-For is an HTTP proxy header that has no business
    authorizing mail at all. Both were treated as sender evidence, which meant
    the allowlist could be satisfied by a field the sender fully controls
    without ever touching the envelope.
    """
    for key in ("envelope_from", "sender", "from", "From"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            return value.get("email") or value.get("address") or ""
    headers = payload.get("headers")
    if isinstance(headers, dict):
        # A forward wraps the original; X-Original-From is set by the forwarder
        # to preserve who really sent it, so a host relaying their FF mail into
        # us still passes. It is only as trustworthy as the forwarding mailbox,
        # which is why it ranks below the envelope.
        for key in ("X-Original-From", "From"):
            if headers.get(key):
                return str(headers[key])
    return ""


# Tags whose *content* is markup rather than prose, and must not reach the body.
_OPAQUE_ELEMENTS = frozenset({"script", "style"})
# End tags that closed a block in the old regex, and so still become a newline.
_BLOCK_END_TAGS = frozenset({"p", "div", "tr"})

# Stand-in for `&` while the parser runs. `html.parser` cannot be asked to leave
# entities alone *and* stay well-behaved: with `convert_charrefs=False` an
# incomplete reference (`?id=9&#details` in a guest's URL) makes it abandon the
# scan and hand back every remaining byte as raw text, so the whole notification
# lands in the body as markup and `parse` finds nothing. Hiding `&` from it
# sidesteps that path entirely and keeps entities byte-exact, which is what the
# old substitution did and what the money/email patterns downstream expect.
# U+E000 is a private-use codepoint; any that somehow arrive are dropped first so
# the round trip cannot invent one.
_AMP_SENTINEL = "\ue000"


class _BodyExtractor(HTMLParser):
    """Turn notification HTML into the plain text the parser downstream reads.

    Replaces a `<[^>]+>` substitution that treated a bare `<` in guest prose as a
    tag opener: "my budget is < $2400" swallowed everything up to the next `>`,
    which is normally the rest of the message — the guest's ask and their reply
    address — plus several real tags. A real parser knows a `<` that no tag name
    follows is text, so it survives (VEN-152).

    Written as a parser rather than a smarter regex because no single tag pattern
    serves the four grammars an ESP actually emits — tags, `<!-- -->` comments,
    `<? ?>` processing instructions and `<![CDATA[ ]]>`. Every regex tried either
    kept eating prose or leaked a comment's innards into the guest-visible body,
    and a leaked prefix is worse than the bug: it un-anchors the label matching in
    `sites.ff_email`, `parse` returns None and the enquiry is dropped outright.

    The unterminated-construct handling exists for the same reason, and is the
    part worth reading twice. Real mail is full of truncated markup, and
    `html.parser` reports an unclosed `<style>` or `<!--` by handing over the
    entire rest of the document as one opaque blob. Dropping that blob — the
    obvious reading of "script bodies are not prose" — silently destroys every
    enquiry whose template has an unclosed `<style>` in its head. So the blob is
    kept and re-stripped as markup instead, which is what the old regex did with
    it. `suppress_opaque=False` marks that second pass and stops it recursing.
    """

    def __init__(self, suppress_opaque: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._opaque: list[str] = []
        self._opaque_depth = 0
        self._suppress_opaque = suppress_opaque
        self._at_eof = False

    # -- collection ---------------------------------------------------------
    def _emit(self, text: str) -> None:
        (self._opaque if self._opaque_depth else self._out).append(text)

    def _recover(self, markup: str) -> None:
        """Salvage a blob the parser could not terminate, as the old regex would.

        Re-stripped rather than emitted raw: the blob is usually real markup, and
        pasting it into the body verbatim un-anchors the `Traveler:` label just as
        badly as dropping it. The nested pass never suppresses, so `<script>` in
        an already-unterminated blob cannot start this over.
        """
        if not markup:
            return
        nested = _BodyExtractor(suppress_opaque=False)
        nested.feed(markup)
        self._out.append(nested.finish())

    def finish(self) -> str:
        self._at_eof = True
        self.close()
        # Still inside an element that never closed: its "body" is the remainder
        # of the email, not a script.
        if self._opaque_depth:
            pending, self._opaque = "".join(self._opaque), []
            self._opaque_depth = 0
            self._recover(pending)
        return "".join(self._out)

    # -- element boundaries -------------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _OPAQUE_ELEMENTS and self._suppress_opaque:
            # Emit the separator before going quiet, so the surrounding words do
            # not run together once the script's body is dropped.
            self._emit(" ")
            self._opaque_depth += 1
            return
        self._emit("\n" if tag == "br" else " ")

    def handle_startendtag(self, tag: str, attrs) -> None:
        # `<br/>` is a start tag that never opens a region; a self-closing
        # script/style likewise has no content to suppress.
        self._emit("\n" if tag == "br" else " ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _OPAQUE_ELEMENTS and self._opaque_depth:
            self._opaque_depth -= 1
            if not self._opaque_depth:
                self._opaque.clear()   # a properly closed script body is not prose
            self._emit(" ")
            return
        self._emit("\n" if tag in _BLOCK_END_TAGS else " ")

    # -- prose --------------------------------------------------------------
    def handle_data(self, data: str) -> None:
        self._emit(data)

    # No `handle_entityref`/`handle_charref`: the `&` sentinel means the parser
    # never sees a reference, so those hooks are unreachable and an override
    # would be untestable dead code. Entities survive because they are hidden
    # from the parser and restored afterwards, not because they are handled.
    # Reconstructing them here is what corrupted ordinary mail: html.parser
    # matches `&name` with no semicolon, so re-emitting `f"&{name};"` turned a
    # guest's "Q&A" into "Q&A;" and moved the message's dedup id.

    # -- markup that is never prose ----------------------------------------
    # Each becomes a separator, never its contents. Outlook puts a conditional
    # comment in nearly every HTML mail it sends, so leaking these is the common
    # case, not an edge one. The `_at_eof` branch is the exception: a construct
    # still open when the document ends is not a comment, it is the rest of the
    # email, and discarding it loses the enquiry.
    def _markup(self, data: str) -> None:
        if self._at_eof:
            self._recover(data)
        else:
            self._emit(" ")

    def handle_comment(self, data: str) -> None:
        # A downlevel-revealed conditional (`<!--[if gte mso 9]>…`) that never
        # closed still opens with a marker, and that marker is not prose.
        if self._at_eof and data.startswith("[if") and ">" in data:
            data = data.split(">", 1)[1]
        self._markup(data)

    def handle_decl(self, decl: str) -> None:
        self._markup(decl)

    def handle_pi(self, data: str) -> None:
        self._markup(data)

    def unknown_decl(self, data: str) -> None:
        # `<![CDATA[ … ` arrives with its opener as part of the payload; that
        # marker is markup even when the section it opened never closed.
        if self._at_eof and data.upper().startswith("CDATA["):
            data = data[len("CDATA["):]
        self._markup(data)


def _legacy_strip_html(value: str) -> str:
    """The pre-VEN-152 substitution, kept only as a last-resort safety net."""
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", value)
    return re.sub(r"<[^>]+>", " ", text)


def strip_html(value: str) -> str:
    """Extract readable text from an HTML mail part. Never raises, never blanks.

    `accept` turns an unparseable body into a `Rejected`, and the webhook still
    answers 202 — so the provider never retries and the enquiry is gone. Every
    failure here has to degrade to "some text" rather than "no text".
    """
    guarded = value.replace(_AMP_SENTINEL, "").replace("&", _AMP_SENTINEL)
    extractor = _BodyExtractor()
    try:
        extractor.feed(guarded)
        text = extractor.finish()
    except Exception:
        # html.parser is lenient by design, so this is belt and braces — but the
        # cost of being wrong is a destroyed enquiry, not a stack trace.
        log.warning("inbound: HTML extraction failed, falling back to tag strip")
        text = _legacy_strip_html(guarded)
    if not text.strip() and value.strip():
        # Whatever happened, returning nothing guarantees the enquiry is dropped.
        # The old substitution is worse at prose but it never blanks a document.
        log.warning("inbound: HTML extraction came back empty, falling back")
        text = _legacy_strip_html(guarded)
    return re.sub(r"[ \t]+", " ", text.replace(_AMP_SENTINEL, "&"))


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
            return strip_html(value)
    return ""


def extract_subject(payload: dict) -> str:
    for key in ("subject", "Subject"):
        if isinstance(payload.get(key), str):
            return payload[key]
    return ""


def extract_date(payload: dict) -> str:
    """When the provider says this mail was sent, for the message id fallback.

    Only used when the notification itself carries no date. Two messages whose
    guest wrote the identical text — "Any update?" sent twice — are otherwise
    indistinguishable, and the second was discarded as already-seen while the
    lifecycle marked the guest lost for not replying.
    """
    for key in ("date", "Date", "timestamp", "Timestamp"):
        value = payload.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    headers = payload.get("headers")
    if isinstance(headers, dict):
        for key in ("Date", "date"):
            if headers.get(key):
                return str(headers[key]).strip()
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

    item = ff_email.parse(extract_subject(payload), extract_body(payload),
                          received_at=extract_date(payload))
    if not item:
        raise Rejected("could not parse a lead from the message")
    return tenant_id, item


def store(tenant_id: str, item: dict, site: str = "furnishedfinder") -> bool:
    """Put an ingested item through the normal pipeline. True if it was new.

    Deliberately the same path a scrape uses — dedup, deal creation and drafting
    behave identically no matter how the lead arrived.

    A *message* that belongs to a conversation we already have joins that deal
    rather than opening a second one beside it. Without this a guest's reply
    became a new deal: the owner saw the same person twice, the reply carried
    none of the original's booking facts, and the nurture sequence on the
    original kept chasing someone who had just written back.
    """
    import config
    import pipeline
    import storage

    kind = item.get("kind", "lead")
    new_items = storage.filter_new(tenant_id, site, kind, [item])
    if not new_items:
        return False
    try:
        parent = None
        if kind == "message":
            parent = pipeline.find_thread(
                tenant_id, site, pipeline.thread_key(item),
                exclude_item_id=item.get("id"))
        if parent:
            pipeline.record_guest_reply(tenant_id, site, parent["item_id"])
        else:
            pipeline.ensure(tenant_id, site, item, None,
                            units=config.get_units(tenant_id))
    except Exception:
        log.exception("Could not open a deal for ingested item %s", item.get("id"))
    return True
