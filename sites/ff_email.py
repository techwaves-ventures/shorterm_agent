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

# The leading lookbehind is what keeps this linear, and it costs no matches.
# `[\w.+-]+@` re-scanned the whole of an unbroken run from every offset inside
# it before concluding there was no `@`, which is O(n^2): 16 KB of `aaaa…`,
# `7777…`, `----`, `....` or base64 cost ~1 s, with the GIL held throughout.
# Requiring the local part to start at a token boundary leaves one viable
# starting offset per run instead of one per character.
#
# It cannot change what `search` finds — which is all this pattern is used for,
# here and in `responder.py`. `search` returns the leftmost match, and a match
# beginning mid-token implies a match beginning at that token's start — the
# greedy `[\w.+-]+` simply absorbs the extra prefix — so every offset this
# prunes was one that could never have produced the leftmost match.
#
# That argument is specific to `search`. It does *not* extend to `findall` or
# `finditer`, which resume scanning at the end of the previous match: a second
# match starting immediately after the first is preceded by a class character
# and so is now suppressed. `'a@b.c+d@e.f'` is the shape. If you ever want all
# matches out of this pattern, re-derive the equivalence before trusting it.
#
# Bounding
# the quantifiers instead (`{1,64}`) was tried and is *not* equivalent: it
# matched a truncated tail of an over-long local part, which would store the
# wrong address on the lead, and dropped addresses whose domain label ran past
# the bound.
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+\.[\w.-]+")
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
    # The value is on this line or within the next two, but the gap is counted
    # in *lines* rather than in whitespace. `\s*\n?\s*` let it be unbounded, so
    # a field the template left empty ("Date received:" with nothing after it)
    # reached arbitrarily far down and took the guest's first sentence as its
    # value — which put message text in `received_at`, and because that stamp is
    # part of a message's id, made two different messages collide whenever the
    # guest opened with the same words.
    #
    # Two lines rather than one: HTML-to-text conversion of the template's table
    # renders a label and its cell as separate lines and sometimes leaves a
    # blank one between. Tightening this to a single line dropped those layouts
    # entirely, and a dropped notification is a lost lead — the webhook answers
    # 202 either way and the provider never retries. The remaining risk of
    # reaching into the guest's text is handled where it matters, by validating
    # the value against the kind of field it claims to be (`_only_if_a_date`,
    # `_only_if_a_stamp`, `_only_if_a_name`).
    return re.compile(
        rf"{prefix}{re.escape(label)}[ \t]*:?[ \t]*(?:\r?\n[ \t]*){{0,2}}(.+)", flags)


# Lines that belong to FurnishedFinder's wrapper rather than to the guest.
_TEMPLATE_LABELS = (
    "property", "listing", "your property", "traveler", "tenant", "guest",
    "from", "name", "date", "date received", "received", "sent", "move in", "move-in",
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


# A forward wraps the original notification in a banner plus an RFC-822 header
# block. Both belong to the forwarding, not to the guest, and leaving them in
# made the same message fingerprint differently depending on who relayed it —
# so a host who forwarded their FurnishedFinder mail twice got two copies of one
# conversation. Matched only near the top of the body, because the same banner
# lower down is quoted history and `_strip_quoted` owns that case.
_FORWARD_BANNER = re.compile(
    r"^\s*(?:-{2,}\s*forwarded message\s*-{2,}|begin forwarded message:?)\s*$",
    re.I,
)
_FORWARD_HEADER = re.compile(r"^\s*(from|to|cc|bcc|date|sent|subject|reply-to)\s*:", re.I)
# How far into the body a banner still counts as "this mail is a forward".
_FORWARD_LOOKAHEAD = 6


def _forward_split(body: str) -> tuple[int, str]:
    """(index after a leading forward header block, the original Date it named).

    Returns (0, "") when this isn't a forward. The inner `Date:` is worth
    keeping: it is the *original* delivery stamp, so using it in the id lets a
    re-forward dedup against the first copy instead of arriving as a new
    message with a fresh transport date.
    """
    lines = (body or "").split("\n")
    for i, line in enumerate(lines[:_FORWARD_LOOKAHEAD]):
        if not _FORWARD_BANNER.match(line):
            continue
        date = ""
        j = i + 1
        while j < len(lines):
            stripped = lines[j].strip()
            if not stripped:
                j += 1
                continue
            header = _FORWARD_HEADER.match(lines[j])
            if not header:
                break
            if header.group(1).lower() in ("date", "sent"):
                date = lines[j].split(":", 1)[1].strip()
            j += 1
        return j, date
    return 0, ""


def _strip_forwarded(body: str) -> str:
    """The original notification, without the banner a forward wrapped it in."""
    start, _ = _forward_split(body)
    return "\n".join((body or "").split("\n")[start:]) if start else (body or "")


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
    for line in _strip_quoted(_strip_forwarded(body)).split("\n"):
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
    """Whitespace-insensitive digest of *what the guest wrote*, for id derivation.

    Fingerprints `_guest_text` rather than the raw email — the same text that
    gets stored and shown. The raw body carries the relaying wrapper too, so one
    message re-forwarded through a different mail client hashed differently and
    arrived as a second copy of itself; the guest's own sentence is the part
    that is actually stable across relays.

    Collapsed rather than exact so re-wrapped lines and padded table cells still
    fingerprint identically, while genuinely different messages stay distinct.
    """
    collapsed = re.sub(r"\s+", " ", _guest_text(body)).strip().lower()
    return hashlib.sha1(collapsed.encode()).hexdigest()[:16]


def _kind_from_subject(subject: str) -> str:
    low = (subject or "").lower()
    if any(h in low for h in _MESSAGE_HINTS) and not any(h in low for h in _LEAD_HINTS):
        return "message"
    return "lead"


# Labels that name the *guest*, and — separately — the one that also names the
# person who forwarded the mail. A forwarded notification carries an RFC-822
# "From:" line above FurnishedFinder's own wrapper, so a single document-order
# pass over all of them let the *host's* name win and put every guest that host
# forwarded onto one shared thread. The specific labels are therefore always
# tried first, and "From" only answers when the wrapper offers nothing better.
_GUEST_LABELS = ("traveler", "tenant", "guest", "name")
_SENDER_LABELS = ("from",)


def _name_line_re(labels: tuple) -> re.Pattern:
    """Anchored `Label: value` matcher, tolerant of the value sitting below it.

    The colon is optional and the value may be up to one blank line below,
    because HTML-to-text conversion of the template's table renders the label
    and its cell as two separate lines — sometimes with the blank line between
    them. Requiring "colon, same line" silently dropped those layouts, and a
    dropped notification is a lost lead: the webhook answers 202 either way and
    the provider never retries.
    """
    return re.compile(
        rf"^[ \t>*|·-]*(?:{'|'.join(labels)})(?![^\W\d_])"
        rf"[ \t]*:?[ \t]*(?:\r?\n[ \t]*){{0,2}}(.+)$",
        re.I | re.M,
    )


_GUEST_NAME_LINE = _name_line_re(_GUEST_LABELS)
_SENDER_NAME_LINE = _name_line_re(_SENDER_LABELS)

# Function words that open a sentence but never a name. Used only to spot a
# labelled value that is really prose — deliberately a *small* list, because the
# cost of a false positive here is a silently discarded lead.
#
# Every entry has to survive one question: is it also somebody's given name?
# "Will", "An", "Or", "New" and "Sent" were all in this list and all rejected a
# real guest ("Will Smith", "An Nguyen", "Or Levi"), so they are gone. That is
# the whole reason this test is additionally gated on the word being lower-case
# as written: a blocklist of English function words will always collide with
# some name somewhere, and the collision must not be what decides.
_PROSE_OPENERS = frozenset(
    ("a", "the", "this", "that", "these", "those", "your", "our", "my",
     "his", "her", "their", "its", "about", "regarding", "from", "with", "for",
     "and", "but", "is", "are", "was", "were", "has", "have", "had",
     "would", "can", "could", "please", "you", "we", "they", "he",
     "she", "it", "there", "here",
     "interested", "replied", "message", "messages"))


def _only_if_a_name(value: str) -> str:
    """A labelled value, or "" if it isn't plausibly a person's name.

    The same guard `_only_if_a_date` applies to dates, and it matters more here:
    this value becomes the deal's `thread_key`, so a junk name doesn't just look
    wrong in the UI — it decides which conversation a message joins. Template
    prose matched the loose label pass and gave two unrelated guests the
    identical name "about your property.", and therefore one shared thread, with
    each of them reading the other's messages.

    Deliberately permissive. The structural fix is what closes the defect —
    a name is only read from an *anchored* label line now, so the wrapper's own
    prose can no longer supply one — and this check is the second line of
    defence against a labelled value that is still a sentence. Getting it wrong
    in the strict direction is expensive and invisible: `parse` returns None for
    a nameless notification, the webhook answers 202, and the provider never
    retries, so an over-eager rule here deletes a real guest's enquiry silently.

    An earlier version required every word to be capitalised. That reads as
    obviously right and is wrong for most of the world: it threw out "李伟",
    "محمد الفارسي", "d'Angelo Smith", any lower-case-rendered name, and anything
    decorated the way this template decorates ("Emma M. (verified)",
    "Emma M. | RN"). It also capped names at four words, discarding
    "Maria del Carmen Garcia Lopez" — and the subject-line fallback then yielded
    a *different* name ("Maria"), which is worse than dropping it, because it
    opens a second deal alongside the one she already has.

    So the only things rejected are the shapes prose actually takes: no words at
    all, a sentence's worth of them, or an opening function word.
    """
    v = (value or "").split("\n")[0].strip().strip("·|-").strip()
    if not v or "@" in v or len(v) > 60:
        return ""
    # A colon means the label pass ran into a *different* field, not a name —
    # "Travelers: 3" yielding "s: 3" is how two guests last shared a thread.
    if ":" in v:
        return ""
    words = v.split()
    # A sentence, not a name. Six allows "Juan Carlos de la Cruz Rodriguez".
    if not 1 <= len(words) <= 6:
        return ""
    # No letters at all — bare punctuation, or an occupancy count like "3".
    # `\w` is not enough here: it accepts digits.
    if not re.search(r"[^\W\d_]", v, re.U):
        return ""
    first = words[0].strip(".,;:!?")
    # Only prose if it *reads* as prose. A capitalised first word is taken as a
    # name even when it collides with a function word, because dropping "Will
    # Smith" loses his enquiry silently and forever, while accepting a stray
    # capitalised word costs one odd-looking thread_key.
    if first.lower() in _PROSE_OPENERS and first[:1] == first[:1].lower():
        return ""
    return v


def _wrapper_name(body: str) -> str:
    """The guest's name as the *wrapper* stated it — first labelled name wins.

    Document order, deliberately not label-preference order, and anchored to the
    start of a line. Both parts are load-bearing:

    `_guest_name` used to try "Traveler" before "Tenant" wherever either
    appeared, and `_label` searches the entire email — including the part the
    guest typed. So a guest whose own message contained the line
    "Traveler: Emma M." was parsed as Emma, threaded onto Emma's conversation,
    and `_guest_text` then stripped that line back out, leaving the operator
    reading what looked like a message from Emma asking for the door code.
    FurnishedFinder's wrapper names the guest once, above the message body, so
    the first labelled name in the document is the wrapper's and any later one
    is the guest quoting or forging.

    The loose (unanchored) pass `_label` keeps for form fields is not used here:
    it is what let the sentence "...from your traveler about your property."
    become a name. A name has a subject-line fallback, so requiring the anchor
    loses nothing that matters.

    Document order applies *within* a label group, never across them. A
    forwarded notification carries an RFC-822 "From:" naming the host who
    forwarded it, above FurnishedFinder's wrapper — so a single pass over every
    label let that host's name beat the real "Traveler:" line, and every guest
    they forwarded collapsed onto one shared conversation. Stripping the forward
    banner is not enough on its own, because plenty of clients prepend the
    header block without one; asking the specific labels first is what actually
    makes it safe.
    """
    text = _strip_forwarded(body or "")
    for pattern in (_GUEST_NAME_LINE, _SENDER_NAME_LINE):
        for match in pattern.finditer(text):
            value = (match.group(1) or "").strip()
            # An empty field says nothing about the guest, and neither does one
            # whose "value" is really the *next* field, picked up because this
            # label's own line was blank. Skip those and keep looking.
            if not value:
                continue
            if _spans_lines(match) and _is_template_field(value):
                continue
            # Otherwise this is the wrapper's answer, and it stands even when it
            # turns out not to be a name. Walking on to the next value that
            # parses is what let a guest choose the answer: the wrapper renders
            # their profile name, so anything that gets this line refused —
            # prose, or a smuggled "Guest:" label — handed the decision to the
            # forged label in their own message.
            return _only_if_a_name(value)
    return ""


def _spans_lines(match: re.Match) -> bool:
    """Whether the value sits on a later line than its label."""
    return "\n" in match.group(0)[:match.start(1) - match.start(0)]


def _is_template_field(value: str) -> bool:
    label, sep, _ = value.partition(":")
    return bool(sep) and label.strip().lower() in _TEMPLATE_LABELS




def _guest_name(subject: str, body: str) -> str:
    """The traveler's name, from the wrapper's own label or the subject line."""
    name = _wrapper_name(body)
    if name:
        return name
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
    # The `(?<![A-Za-z])` lookbehinds keep this linear. Unbounded, `[a-z]*`
    # walked to the end of an unbroken letter run from every offset inside it
    # looking for a following digit, which is O(n^2) -- 16 KB of `aaaa…` cost
    # ~4.6 s, with the GIL held throughout. Requiring a month to begin at a
    # letter boundary leaves one viable starting offset per word.
    #
    # This cannot change what is found: `[A-Za-z]{3}` matches any letters, so a
    # match beginning mid-word implies one beginning at that word's start, and
    # `search` returns the leftmost match — so the pruned offsets were never
    # the ones reported. Two near misses are worth keeping in mind if this is
    # ever touched again:
    #
    #   * The lookbehind belongs *inside* the alternation, on the month branch
    #     only. In front of the whole group it also guards `\d{1,2}/\d{1,2}/…`,
    #     whose leading digit has no such absorb-the-prefix property: it turned
    #     "abc12/1/26 - 3/4/27" into "2/1/26" and dropped "x9/1/26 to 12/31/26"
    #     entirely.
    #   * Capping the quantifier instead (`[a-z]{0,7}`) is not equivalent
    #     either — on "Septemberish 3, 2026" it reports the tail "ish 3, 2026".
    #   * Only the *first* group takes a lookbehind. The second is not scanned
    #     for — it is matched at a fixed point after the separator — so the
    #     absorb-the-prefix argument does not licence pruning there, and a word
    #     separator abutting the month made the lookbehind see the separator's
    #     own last letter and veto the whole range: "Jan 5, 2026 toJun 9, 2026"
    #     returned nothing at all. It bought no speed either; the first group
    #     alone is what bounds the scan.
    #
    # All three silently change the *stated* dates, which feed the item id.
    rng = re.search(
        r"((?<![A-Za-z])[A-Za-z]{3}[a-z]*\.?\s+\d{1,2},?\s*\d{4}|\d{1,2}/\d{1,2}/\d{2,4})"
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


def _only_if_a_stamp(value: str) -> str:
    """A labelled value, or "" if it can't be a date/time the template printed.

    The received stamp feeds the message id, so letting the guest's own first
    sentence land here made two different messages hash identically. A stamp
    always carries a digit and either a month name or a date/time separator;
    "Any update?" carries neither. Shape-checked rather than parsed, because
    FurnishedFinder prints several formats and an unrecognised-but-real one is
    still better evidence than nothing.
    """
    v = (value or "").strip()
    if not v or len(v) > 60 or not re.search(r"\d", v):
        return ""
    has_month = re.search(r"[A-Za-z]{3}", v) and re.search(
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", v, re.I)
    return v if (has_month or re.search(r"[/:-]", v)) else ""


def parse(subject: str, body: str, received_at: str = "") -> dict | None:
    """Turn a FurnishedFinder notification into an item, or None if it isn't one.

    Returning None is the safe default: a message we can't confidently read is
    dropped rather than turned into a half-empty lead the agent would write to.

    `received_at` is the transport delivery stamp (the mail `Date` header). It is
    only ever a *fallback* for the message id — see the id derivation below.
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

    received = _only_if_a_stamp(_label(body, "Date received", "Received", "Sent"))
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
    #
    # For a message the stamp is resolved in order of how stable it is across
    # relays, because the id has to satisfy two opposing requirements at once:
    # the same message arriving twice must collapse, and a guest sending the
    # *same words* again ("Any update?") must not. Only a timestamp separates
    # the second case, and only a timestamp that survives forwarding keeps the
    # first. So: FurnishedFinder's own "Date received" line, else the original
    # Date carried inside a forwarded header block, else the transport stamp.
    # If none exists the two collapse — there is genuinely nothing to tell them
    # apart — which is the old behaviour, now confined to a template that
    # carries no date at all.
    parts = [name, stated_in, stated_out, property_name, kind]
    if kind == "message":
        stamp = received or _forward_split(body)[1] or (received_at or "")
        parts += [stamp, _body_fingerprint(body)]
    item["id"] = hashlib.sha1("||".join(parts).encode()).hexdigest()[:16]
    return item
