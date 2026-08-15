"""Parse a FurnishedFinder notification email into the item shape the app uses.

This is the reading path that replaces scraping: FurnishedFinder emails the host
when a lead or message arrives, and that email carries enough to open the deal
and draft a first reply — without a browser ever touching their site.

The output matches what `sites/furnishedfinder.py` produces, so everything
downstream (dedup, deals, sequences, drafting) is identical regardless of how
the lead arrived.

## On fidelity

A notification email is *lower fidelity* than the detail-page scrape. The scrape
yields occupants, pets, budget, reason for travel, occupation and employer — the
facts that make a draft specific. If the email doesn't carry them, drafts will
be more generic. `parse()` therefore records `source: "email"` and only the
fields it genuinely found, so a later enrichment pass can tell the difference
between "not stated" and "not yet fetched".

## On the layout

FurnishedFinder's exact template isn't pinned here — matching is label-driven
and tolerant of order, extra whitespace, and HTML-to-text conversion. Anything
it can't find is simply absent rather than guessed, and `parse()` returns None
when it can't establish the basics, so a mis-parse never becomes a fake lead.
"""
import hashlib
import logging
import re

log = logging.getLogger(__name__)

SITE_NAME = "furnishedfinder"
LEADS_URL = "https://www.furnishedfinder.com/members/tenant-lead"
MESSAGES_URL = "https://www.furnishedfinder.com/members/tenant-message"

# Subjects that mean "a tenant wrote to you" rather than "a new lead arrived".
_MESSAGE_HINTS = ("message", "replied", "reply from", "new message")
_LEAD_HINTS = ("lead", "inquiry", "enquiry", "interested", "booking request")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
_MONEY_RE = re.compile(r"\$\s?[\d,]{3,}")

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _norm_date(value: str) -> str:
    """'Jun 13, 2026' / '6/13/2026' -> 'M/D/YY', matching the scraper's style."""
    value = (value or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", value)
    if m:
        year = int(m.group(3))
        year = year if year < 100 else year % 100
        return f"{int(m.group(1))}/{int(m.group(2))}/{year:02d}"
    m = re.match(r"^([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{1,2}),?\s*(\d{4})$", value)
    if m:
        mon = _MONTHS.get(m.group(1).lower())
        if mon:
            return f"{mon}/{int(m.group(2))}/{m.group(3)[-2:]}"
    return value


def _label(text: str, *labels: str) -> str:
    """Value for a 'Label: value' or 'Label\\nvalue' pair, whichever appears.

    Tolerant of the whitespace mangling that HTML-to-text conversion produces.

    A label is looked for at the start of a line first, and only then anywhere in
    the text. Order matters because these words also occur in the template's own
    prose and in the guest's message: "You have a new message from your
    traveler." matched the *Traveler* label and yielded "." as the guest's name,
    which then became the deal's guest_name and left the conversation with no
    identity to thread on. The loose pass is kept because HTML-to-text
    conversion does sometimes run a label into the previous cell, so requiring
    the anchor outright would lose real fields.
    """
    for anchored in (True, False):
        for label in labels:
            for match in _label_re(label, anchored).finditer(text):
                value = match.group(1).strip()
                # A label sitting alone on its line means the value is the next one.
                if not value or value.lower().startswith(
                        tuple(l.lower() for l in labels)):
                    continue
                # Trim layout punctuation but keep a trailing period — it's the
                # surname initial in FurnishedFinder's "Emma M." format.
                value = value.split("\n")[0].strip().strip("·|-").strip()
                # Bare punctuation is the tail of a sentence, not a value.
                if value and re.search(r"[A-Za-z0-9]", value):
                    return value
    return ""


def _label_re(label: str, anchored: bool) -> re.Pattern:
    """Matcher for one label, optionally required to start its own line.

    The anchor tolerates the quote markers and bullet padding that forwarding
    and HTML-to-text conversion prepend to a line.
    """
    prefix = r"^[ \t>*|·-]*" if anchored else ""
    flags = re.I | (re.M if anchored else 0)
    return re.compile(rf"{prefix}{re.escape(label)}\s*:?\s*\n?\s*(.+)", flags)


# Lines that belong to FurnishedFinder's wrapper rather than to the guest.
_TEMPLATE_LABELS = (
    "property", "listing", "your property", "traveler", "tenant", "guest",
    "from", "name", "date received", "received", "sent", "move in", "move-in",
    "move out", "move-out", "check in", "check-in", "check out", "check-out",
    "travelers", "occupants", "guests", "number of guests", "budget",
    "max budget", "price range", "traveling with pets", "pets", "nights",
    "reason for travel", "occupation", "work location", "start date",
    "end date", "arrival", "departure", "requested travel dates", "subject",
)
_BOILERPLATE = re.compile(
    r"^(you have a new|reply to this|view this|log ?in to|click here|"
    r"this message was sent|do not reply|unsubscribe|sent from)",
    re.I,
)

# Where the guest's own message stops and the history they replied on top of
# begins. Everything from the first of these to the end of the body is the
# previous conversation (or a mail-client signature), not this message.
#
# Every marker here has to be unambiguous, because cutting at the wrong line
# silently deletes what the guest said. A run of dashes is specifically NOT a
# marker: FurnishedFinder uses those as layout dividers inside its own wrapper,
# above the guest's text, so cutting there would drop the entire message. Only
# the exact RFC 3676 signature delimiter ("--" alone on its line) qualifies.
_QUOTE_START = re.compile(
    r"^\s*(>|--\s*$|"
    r"on\s.{0,120}\swrote\s*:\s*$|"
    r"-+\s*original message\s*-+|"
    r"from\s*:\s.+\bsent\s*:|"
    r"begin forwarded message)",
    re.I,
)


def _strip_quoted(body: str) -> str:
    """Drop the quoted history and signature a reply is stacked on top of.

    A guest replying from their mail client sends their sentence followed by the
    entire prior thread — including our own last message. Storing all of it made
    the thread view show us our own words back, attributed to the guest, and
    grew without bound as the conversation went on.
    """
    lines = (body or "").split("\n")
    for i, line in enumerate(lines):
        if _QUOTE_START.match(line):
            return "\n".join(lines[:i]).rstrip()
    return body or ""


def _is_wrapper_value(value: str) -> bool:
    """Whether `Label: <value>` is a template field rather than the guest talking.

    The label filter used to drop any line whose first word matched a template
    field, wherever it appeared. Guests answer in exactly those words, so
    "Budget: I can do $2000." and "Pets: yes, one cat" were deleted from the
    message before it was ever shown — the operator saw a reply with the single
    most important sentence missing.

    A wrapper value is short and declarative: "$2,400/month", "Yes", "8/16/26".
    Once it runs to a sentence, it is prose, and prose belongs to the guest.
    """
    v = (value or "").strip()
    if not v:
        return True
    if len(v) > 60:
        return False
    # A sentence's worth of words, or sentence punctuation, means they wrote it.
    if re.search(r"[.!?]\s|[.!?]$", v) and len(v.split()) > 2:
        return False
    # A comma that separates words is someone elaborating ("yes, one cat")
    # rather than a form field. Commas that precede a number are punctuation
    # inside a value — "$2,400/month", "July 19, 2026" — and stay wrapper.
    if re.search(r",(?!\s*\d)", v) and len(v.split()) > 2:
        return False
    return not re.search(r"\b(i|i'm|im|we|we're|my|our|can|could|would|please)\b",
                         v, re.I)


def _guest_text(body: str) -> str:
    """Just what the guest actually wrote, without the notification wrapper.

    The whole email used to be stored as the message body, so the thread view
    would show the guest saying "You have a new message from your traveler.
    Property: ... Traveler: ..." before getting to their actual sentence. That
    is the wrapper talking, not them.

    Conservative on purpose: it drops lines that are a known template field or
    recognisable boilerplate and keeps everything else, so an unfamiliar layout
    degrades to showing too much rather than swallowing the message. If nothing
    survives, the caller still has the untrimmed text.
    """
    kept = []
    for line in _strip_quoted(body).split("\n"):
        stripped = line.strip().strip("·|").strip()
        if not stripped:
            kept.append("")
            continue
        if _BOILERPLATE.match(stripped):
            continue
        label = stripped.split(":", 1)[0].strip().lower() if ":" in stripped else ""
        if label and label in _TEMPLATE_LABELS:
            if not _is_wrapper_value(stripped.split(":", 1)[1]):
                kept.append(stripped)
            continue
        if stripped.lower() in _TEMPLATE_LABELS:
            continue
        kept.append(stripped)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return text or (body or "").strip()


def _body_fingerprint(body: str) -> str:
    """Whitespace-insensitive digest of a message body, for id derivation.

    Collapsed rather than raw so the same message re-forwarded through a
    different HTML-to-text conversion (which re-wraps lines and pads cells)
    still fingerprints identically and dedups, while genuinely different
    messages from the same guest stay distinct.
    """
    collapsed = re.sub(r"\s+", " ", (body or "")).strip().lower()
    return hashlib.sha1(collapsed.encode()).hexdigest()[:16]


def _kind_from_subject(subject: str) -> str:
    low = (subject or "").lower()
    if any(h in low for h in _MESSAGE_HINTS) and not any(h in low for h in _LEAD_HINTS):
        return "message"
    return "lead"


def _guest_name(subject: str, body: str) -> str:
    """The traveler's name, from an explicit label or the subject line."""
    for label in ("Traveler", "Tenant", "Guest", "From", "Name"):
        value = _label(body, label)
        # Reject an address that leaked in where a name was expected.
        if value and "@" not in value and len(value) < 60:
            return value.strip()
    # "New lead from Emma M." / "Emma M. sent you a message"
    m = re.search(r"(?:from|by)\s+([A-Z][\w'’-]+(?:\s+[A-Z][\w'’.-]*)?)", subject or "")
    if m:
        return m.group(1).strip()
    m = re.match(r"\s*([A-Z][\w'’-]+(?:\s+[A-Z][\w'’.-]*)?)\s+(?:sent|messaged|is)", subject or "")
    if m:
        return m.group(1).strip()
    return ""


def _dates(text: str) -> tuple[str, str]:
    """(move_in, move_out) as stated, normalized but NOT validated.

    "As stated" matters: the caller uses these to decide whether the email is a
    real notification at all, and to derive the item id. A traveler who writes
    "ASAP" or "Flexible" in the move-in field has still stated something, and
    two leads that differ only there are still two leads. Use `_only_if_a_date`
    on the way to storage, where a non-date is worse than nothing.
    """
    rng = re.search(
        r"([A-Za-z]{3}[a-z]*\.?\s+\d{1,2},?\s*\d{4}|\d{1,2}/\d{1,2}/\d{2,4})"
        r"\s*(?:-|–|—|to|through|until)\s*"
        r"([A-Za-z]{3}[a-z]*\.?\s+\d{1,2},?\s*\d{4}|\d{1,2}/\d{1,2}/\d{2,4})",
        text, re.I,
    )
    if rng:
        return _norm_date(rng.group(1)), _norm_date(rng.group(2))
    move_in = _label(text, "Move in", "Move-in", "Check in", "Check-in", "Start date", "Arrival")
    move_out = _label(text, "Move out", "Move-out", "Check out", "Check-out", "End date", "Departure")
    return _norm_date(move_in), _norm_date(move_out)


def _only_if_a_date(value: str) -> str:
    """Normalize a labelled value, or drop it if it isn't actually a date.

    `_label` matches anywhere in the text, including inside a guest's own prose.
    A traveler writing "can I move in a week earlier?" would otherwise set
    move_in to "a week earlier?" — junk that lands on the deal, fails date
    parsing downstream, and reads as a real requested date in the UI. A value
    that doesn't normalize to a calendar date is not one.
    """
    normalized = _norm_date(value)
    return normalized if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2}", normalized or "") else ""


def parse(subject: str, body: str) -> dict | None:
    """Turn a FurnishedFinder notification into an item, or None if it isn't one.

    Returning None is the safe default: a message we can't confidently read is
    dropped rather than turned into a half-empty lead the agent would write to.
    """
    body = (body or "").replace("\r\n", "\n").replace("\xa0", " ")
    subject = (subject or "").strip()
    if not body.strip():
        return None

    kind = _kind_from_subject(subject)
    name = _guest_name(subject, body)
    # `stated_*` is what the email said; `move_*` is what we're willing to store
    # as a date. They differ for ordinary FurnishedFinder values like "ASAP" and
    # "Flexible": those are not dates, but they are evidence that this is a real
    # lead notification, and they still distinguish one lead from another. The
    # gate and the id below therefore use the stated values — validating first
    # would silently discard a lead whose guest typed "Flexible", and would
    # collapse two such leads onto one id.
    stated_in, stated_out = _dates(body)
    move_in, move_out = _only_if_a_date(stated_in), _only_if_a_date(stated_out)
    property_name = _label(body, "Property", "Listing", "Your property")

    # Require enough to be a real notification: someone to talk to, plus at
    # least one concrete fact. Otherwise this is a newsletter or a digest.
    if not name or not (stated_in or property_name or kind == "message"):
        log.info("Inbound email didn't look like a lead/message (subject=%r)", subject[:80])
        return None

    item: dict = {
        "kind": kind,
        "title": " | ".join(x for x in (property_name, name) if x) or subject[:200],
        "url": MESSAGES_URL if kind == "message" else LEADS_URL,
        # Marks the lower-fidelity path so a later pass can tell "not stated"
        # from "not fetched" (see the module docstring).
        "source": "email",
        "raw": body[:4000],
    }
    if kind == "message":
        item["sender"] = name
    else:
        item["traveler"] = name
    if property_name:
        item["property_name"] = property_name
    if move_in:
        item["move_in"] = move_in
    if move_out:
        item["move_out"] = move_out

    received = _label(body, "Date received", "Received", "Sent")
    if received:
        item["received_at"] = received

    nights = re.search(r"(\d{1,4})\s*nights?", body, re.I)
    if nights:
        item["nights"] = int(nights.group(1))

    travelers = _label(body, "Travelers", "Occupants", "Guests", "Number of guests")
    if travelers.isdigit():
        item["occupants"] = int(travelers)

    pets = _label(body, "Traveling with pets", "Pets")
    if pets:
        item["pets"] = pets.strip().lower() not in ("no", "none", "false", "0", "-")

    budget = _label(body, "Budget", "Max budget", "Price range")
    if budget and budget != "-":
        item["budget"] = budget
    elif _MONEY_RE.search(body):
        item["budget"] = _MONEY_RE.search(body).group(0)

    for label, key in (("Reason for travel", "reason"), ("Occupation", "occupation"),
                       ("Work location", "work_location")):
        value = _label(body, label)
        if value and value != "-":
            item[key] = value

    email = _EMAIL_RE.search(body)
    # Skip FurnishedFinder's own addresses — we want the traveler's.
    if email and "furnishedfinder" not in email.group(0).lower():
        item["email"] = email.group(0)
    phone = _PHONE_RE.search(body)
    if phone:
        item["phone"] = phone.group(0).strip()

    if kind == "message":
        item["body"] = _guest_text(body)[:4000]
        # Keep the untrimmed notification too — if the trim ever eats something
        # it shouldn't, the original is still there to fall back on.
        item["raw"] = body[:4000]

    # Stable id from the facts that identify this inquiry, so the same
    # notification arriving twice (a re-forward) dedups against itself.
    #
    # A lead is identified by who + when + which property. A *message* is not:
    # the date fields are empty on the message template, so hashing the same
    # five fields made every message from one guest collide onto a single id and
    # `storage.filter_new` discarded all but the first as already-seen — the
    # agent could read a guest's opening message and never their reply. Messages
    # therefore hash over what actually distinguishes them: the received stamp
    # and the body. Both are read out of the email text (not the delivery clock),
    # so a re-forward of the same message still hashes identically and dedups.
    # Hashed on the *stated* dates, not the validated ones, for two reasons:
    # ids stay byte-identical to what this parser produced before validation
    # existed (so no open deal is orphaned), and two leads from one guest that
    # differ only in an unlabelled move-in stay two leads.
    parts = [name, stated_in, stated_out, property_name, kind]
    if kind == "message":
        parts += [received, _body_fingerprint(body)]
    item["id"] = hashlib.sha1("||".join(parts).encode()).hexdigest()[:16]
    return item
