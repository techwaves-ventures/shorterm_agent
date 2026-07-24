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
    """
    for label in labels:
        pattern = re.compile(
            rf"{re.escape(label)}\s*:?\s*\n?\s*(.+)", re.I
        )
        for line in pattern.finditer(text):
            value = line.group(1).strip()
            # A label sitting alone on its line means the value is the next one.
            if value and not value.lower().startswith(tuple(l.lower() for l in labels)):
                # Trim layout punctuation but keep a trailing period — it's the
                # surname initial in FurnishedFinder's "Emma M." format.
                return value.split("\n")[0].strip().strip("·|-").strip()
    return ""


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
    """(move_in, move_out) from a range or separate labels."""
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
    move_in, move_out = _dates(body)
    property_name = _label(body, "Property", "Listing", "Your property")

    # Require enough to be a real notification: someone to talk to, plus at
    # least one concrete fact. Otherwise this is a newsletter or a digest.
    if not name or not (move_in or property_name or kind == "message"):
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
        item["body"] = body[:4000]

    # Stable id from the facts that identify this inquiry, so the same
    # notification arriving twice (a re-forward) dedups against itself.
    item["id"] = hashlib.sha1(
        "||".join([name, move_in, move_out, property_name, kind]).encode()
    ).hexdigest()[:16]
    return item
