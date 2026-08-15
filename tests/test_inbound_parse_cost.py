"""VEN-144: the inbound parse path must stay linear, and the size cap must hold.

Three regexes reachable from `/inbound/email` were O(n^2), and `re.search`
holds the GIL for its whole duration. `Procfile` runs `gunicorn --workers 1
--threads 8 --timeout 120`, so one message of the right shape does not merely
make one request slow — it freezes the only worker, `/healthz` included, until
the arbiter SIGKILLs it and drops every in-flight request.

Nothing gates the cost: `_dates(body)` runs at `sites/ff_email.py:568`, *before*
the "is this a real notification" check below it. The only precondition is a
non-empty body.

Why these tests are shaped the way they are:

* **Ratios, not stopwatches.** The absolute numbers move with the machine; the
  4x-per-doubling signature does not. Each cost test measures n and 2n and
  asserts the ratio, with only a very loose absolute ceiling as a backstop.
* **A sweep of shapes, not one input.** The original investigation reported
  `_dates` as "the sole hot spot" because its probe body was not FF-shaped and
  bailed out before reaching the other two. Every body here is asserted to
  clear the notification gate, and `dashes`/`dots` are included because `-` and
  `.` are inside `_EMAIL_RE`'s character class — they blow up exactly like
  letters and were missing from the original trigger list.
* **`lt_spaced` earns its place.** `"< " * n` has whitespace every other
  character, so it survives any "reject long unbroken runs" input filter. It
  is the case proving `extract_body` needed a real fix rather than a guard.
* **Equality tests alongside the cost tests.** A faster regex that quietly
  stops finding a traveler's email address loses lead data while every timing
  number improves, so the pinned values below matter more than the ratios.
  The bounds that were tried first failed exactly here: `[\\w.+-]{1,64}` matched
  a *truncated tail* of an over-long local part, and `[\\w-]{1,63}` dropped
  addresses with a long domain label.

Verified against `6f62a57`: every cost test below fails there (ratios ~4x), and
every equality test passes there — the equality tests are pinning behaviour the
fix must preserve, not behaviour it introduces.
"""
import itertools
import json
import os
import tempfile
import time

import pytest

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("INBOUND_EMAIL_DOMAIN", "inbound.example.com")
os.environ.setdefault("INBOUND_WEBHOOK_SECRET", "hook-secret")

import inbound  # noqa: E402
from sites import ff_email  # noqa: E402

# A body that genuinely parses, so the cost is measured on the path a real
# notification takes rather than on an early return.
FF_BODY = (
    "You have a new lead on FurnishedFinder.\n"
    "Traveler: Jordan Keller\n"
    "Property: 123 Maple St Unit B\n"
    "Move in: Mar 3, 2026\n"
    "Move out: Jun 30, 2026\n"
    "Travelers: 2\n"
    "Message: {payload}\n"
)

# Shapes that drove a quantifier to end-of-input from every offset.
PATHOLOGICAL = {
    "letters": lambda n: "a" * n,
    "caps": lambda n: "A" * n,
    "digits": lambda n: "7" * n,
    "base64": lambda n: ("QWxhZGRpbjpvcGVuIHNlc2FtZQ" * (n // 26 + 1))[:n],
    "dashes": lambda n: "-" * n,
    "dots": lambda n: "." * n,
    "word_chars": lambda n: ("Ab3_" * (n // 4 + 1))[:n],
    "at_runs": lambda n: (("a" * 60 + "@") * (n // 61 + 1))[:n],
}

HTML_PATHOLOGICAL = {
    "lt_runs": lambda n: "<" * n,
    "lt_spaced": lambda n: ("< " * (n // 2 + 1))[:n],
    "script_runs": lambda n: ("<script" * (n // 7 + 1))[:n],
}

# Ratio a doubling of input may cost. Linear is ~2, quadratic is ~4.
MAX_RATIO = 3.0
# Backstop only. Quadratic blows through this by orders of magnitude.
MAX_SECONDS = 5.0


def _best(fn, arg, repeat=3):
    """Fastest of `repeat` runs — the least noisy estimator of real cost."""
    best = float("inf")
    for _ in range(repeat):
        start = time.perf_counter()
        fn(arg)
        best = min(best, time.perf_counter() - start)
    return best


@pytest.mark.parametrize("shape", sorted(PATHOLOGICAL))
def test_parse_stays_linear_in_body_length(shape):
    make = PATHOLOGICAL[shape]

    def run(n):
        body = FF_BODY.format(payload=make(n))
        item = ff_email.parse("New lead from Jordan", body)
        # Precondition: a body that bailed out early would make any timing
        # below meaningless, which is how the first investigation missed two
        # of the three hot spots.
        assert item is not None, f"{shape}: body did not reach the parse path"
        return item

    small = _best(run, 8000)
    large = _best(run, 16000)
    assert large < MAX_SECONDS, f"{shape}: {large:.2f}s for a 16 KB body"
    assert large / small < MAX_RATIO, (
        f"{shape}: doubling the body multiplied cost by {large / small:.1f}x "
        f"({small * 1000:.0f}ms -> {large * 1000:.0f}ms); quadratic is ~4x"
    )


@pytest.mark.parametrize("shape", sorted(HTML_PATHOLOGICAL))
def test_html_fallback_stays_linear(shape):
    make = HTML_PATHOLOGICAL[shape]

    def run(n):
        text = inbound.extract_body({"html": "<p>Traveler: Jordan</p>" + make(n)})
        assert text, f"{shape}: html fallback produced nothing"
        return text

    small = _best(run, 8000)
    large = _best(run, 16000)
    assert large < MAX_SECONDS, f"{shape}: {large:.2f}s for 16 KB of html"
    assert large / small < MAX_RATIO, (
        f"{shape}: doubling the html multiplied cost by {large / small:.1f}x "
        f"({small * 1000:.0f}ms -> {large * 1000:.0f}ms); quadratic is ~4x"
    )


def test_ordinary_prose_was_never_the_problem():
    """The control. Normal wrapped text stays cheap on both sides of the fix,
    so a passing suite above means "the pathology is gone", not "everything got
    faster"."""
    body = FF_BODY.format(payload=("the quick brown fox jumps over a lazy dog " * 400))
    assert ff_email.parse("New lead from Jordan", body) is not None
    assert _best(lambda b: ff_email.parse("New lead from Jordan", b), body) < 1.0


# --- behaviour that must survive the rewrite ------------------------------

# Values as produced by `6f62a57`. `_dates` reports what the mail *stated*,
# normalized but not validated, so these are m/d/yy rather than ISO.
@pytest.mark.parametrize("text,expected", [
    ("Requested travel dates: Mar 3, 2026 - Jun 30, 2026", ("3/3/26", "6/30/26")),
    ("Sept. 1, 2026 – Dec 15, 2026", ("9/1/26", "12/15/26")),
    ("September 1, 2026 through December 15, 2026", ("9/1/26", "12/15/26")),
    ("Jan 5, 2027 until Mar 1, 2027", ("1/5/27", "3/1/27")),
    ("Nov 30, 2026-Dec 31, 2026", ("11/30/26", "12/31/26")),
    ("3/1/2026 - 6/30/26", ("3/1/26", "6/30/26")),
    ("12/1/26 to 3/15/27", ("12/1/26", "3/15/27")),
    ("10/15/2026 through 04/15/2027", ("10/15/26", "4/15/27")),
])
def test_date_ranges_still_parse(text, expected):
    assert ff_email._dates(text) == expected


@pytest.mark.parametrize("text,expected", [
    # Digit-led dates preceded by letters. The lookbehind that makes the month
    # branch linear must sit *inside* the alternation: in front of the whole
    # group it also guarded this branch, turning the first into "2/1/26" and
    # dropping "x9/1/26 to 12/31/26" altogether.
    ("ref12/1/2026 through 6/30/2027", ("12/1/26", "6/30/27")),
    ("abc12/1/26 - 3/4/27", ("12/1/26", "3/4/27")),
    ("x9/1/26 to 12/31/26", ("9/1/26", "12/31/26")),
])
def test_digit_dates_are_not_truncated_by_the_lookbehind(text, expected):
    assert ff_email._dates(text) == expected


@pytest.mark.parametrize("text,expected", [
    # A word separator running straight into the second month. Only the *first*
    # group may take a lookbehind: the second is matched at a fixed point after
    # the separator rather than scanned for, so a lookbehind there saw the
    # separator's own last letter ("to|Jun") and vetoed the entire range —
    # `_dates` returned ("", "") and the lead lost both dates, or was dropped
    # outright when the notification had no property line to fall back on.
    ("Jan 5, 2026 toJun 9, 2026", ("1/5/26", "6/9/26")),
    ("Jan 5, 2026 throughMar 1, 2027", ("1/5/26", "3/1/27")),
    ("Jan 5, 2026 untilMar 1, 2027", ("1/5/26", "3/1/27")),
    (" 9/9/26toMar. 31, 2026", ("9/9/26", "3/31/26")),
])
def test_word_separator_abutting_the_month_still_matches(text, expected):
    assert ff_email._dates(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("Contact: jordan.keller@gmail.com", "jordan.keller@gmail.com"),
    ("j+ff@sub.example.co.uk", "j+ff@sub.example.co.uk"),
    ("first_last@company-name.com", "first_last@company-name.com"),
    ("name.with.many.dots@example.museum", "name.with.many.dots@example.museum"),
    ("user+tag+more@mail.example.org", "user+tag+more@mail.example.org"),
    ("Reach me at jane@doe.net or call", "jane@doe.net"),
    ("meet @ 5pm, no address here", None),
    # The two cases that killed the bounded-quantifier attempt.
    ("x" * 70 + "@example.com", "x" * 70 + "@example.com"),
    ("kim@" + "d" * 70 + ".com", "kim@" + "d" * 70 + ".com"),
])
def test_email_addresses_are_found_whole(text, expected):
    m = ff_email._EMAIL_RE.search(text)
    assert (m.group(0) if m else None) == expected


@pytest.mark.parametrize("html,must_contain", [
    ("<p>Hello <b>Jordan</b></p>", "Jordan"),
    ("<style>.a{color:red}</style><p>Body</p>", "Body"),
    ("<script>var x=1;</script><p>Body</p>", "Body"),
    ("<SCRIPT>alert(1)</SCRIPT>text", "text"),
    ("<table><tr><td>Traveler</td><td>Jordan</td></tr></table>", "Jordan"),
    ("line one<br>line two", "line two"),
    ("unclosed <script>var x=1; and nothing after", "nothing after"),
])
def test_html_fallback_still_yields_the_text(html, must_contain):
    assert must_contain in inbound.extract_body({"html": html})


def test_script_content_is_still_removed():
    assert "secret" not in inbound.extract_body(
        {"html": "<p>hi</p><script>var secret=1;</script>"})


def test_item_id_is_unchanged_by_the_rewrite():
    """The stated dates and the extracted address feed the item id, so a regex
    that matches a *different* substring silently splits one lead into two (or
    collapses two into one). These ids were taken from `6f62a57`."""
    item = ff_email.parse("New lead from Jordan", FF_BODY.format(
        payload="I am a travel nurse. Reach me at jordan.keller@gmail.com"))
    assert item["move_in"] == "3/3/26"
    assert item["move_out"] == "6/30/26"
    assert item["email"] == "jordan.keller@gmail.com"
    assert item["id"] == "97af61eecfd80071"


# --- non-ASCII: the case-folding trap -------------------------------------
#
# Every other equality test in this file is ASCII, and that is exactly why the
# first version of `_script_spans` shipped green while corrupting real mail.
# It lowercased the input and then indexed the *original* with the result.
# `str.lower()` is not length-preserving: U+0130 (`İ`, Turkish dotted capital
# I) folds to two characters, and it is the only codepoint in Unicode that
# does. Each one before a closing tag dragged the computed span one character
# too far. Expected values below were produced by the old pipeline on 6f62a57.

@pytest.mark.parametrize("html,expected", [
    # Overshoot eats text that follows the element.
    ("<script>İİİ</script>TAIL", " TAIL"),
    # The `İ` need not be inside the element — anything before the closer
    # shifts it, so ordinary Turkish prose plus any <style> block triggers it.
    ("<p>Hi İlker from İstanbul</p><style>p{color:red}</style>"
     "<p>Budget is $2000</p>",
     " Hi İlker from İstanbul\n Budget is $2000\n"),
    # Enough drift skips the next element's opener entirely, which leaks the
    # script content `_SCRIPT_OPEN_RE`'s comment exists to keep out.
    ("<script>" + "İ" * 12 + "</script><script>SECRET_TOKEN=1</script>"
     "<p>hi</p>", " hi\n"),
    # Other non-ASCII must be untouched; only U+0130 changes length.
    ("<p>Größe: 30m²</p><style>x</style><p>ok</p>", " Größe: 30m²\n ok\n"),
    ("<p>Приве́т</p><script>var s=1;</script><p>ok</p>", " Приве́т\n ok\n"),
])
def test_case_folding_does_not_shift_element_spans(html, expected):
    assert inbound.extract_body({"html": html}) == expected


def test_script_content_is_not_leaked_past_a_dotted_capital_i():
    """Exactly 12 `İ` is the discriminating count, and that is the point.

    The drift is one character per `İ`, so *more* of them overshoot the whole
    following element and delete the secret along with it — which reads as a
    pass. 12 lands the span end inside `SECRET_TOKEN=1`, so the tail leaks.
    """
    body = inbound.extract_body(
        {"html": "<script>" + "İ" * 12 + "</script>"
                 "<script>SECRET_TOKEN=1</script><p>hi</p>"})
    assert "TOKEN=1" not in body
    assert body == " hi\n"


def test_message_item_id_is_unchanged_by_a_dotted_capital_i():
    """Must be a *message*, not a lead: only `kind == "message"` mixes
    `_body_fingerprint(body)` into the id (`ff_email.py:667-670`), so the same
    assertion on a lead passes even with the span drift present and proves
    nothing. Id pinned from `6f62a57`; the pre-fix branch gave 4dea5190239c885a.
    """
    html = ("<p>You have a new message on FurnishedFinder.</p>"
            "<p>Traveler: İlker Yılmaz</p>"
            "<p>Property: 123 Maple St Unit B</p>"
            "<style>p{color:red}</style>"
            "<p>Message: Is the unit still available in March?</p>")
    body = inbound.extract_body({"html": html})
    assert "p>Message" not in body
    item = ff_email.parse("New message from İlker", body)
    assert item["id"] == "533bfdca296f208a"


# `re` compares a backreference under IGNORECASE with *simple* (1:1) case
# mapping, but compares a *literal* with *extended* folding:
#
#     re.search(r"(?i)(s)\1", "sſ")  -> None    (backreference)
#     re.search(r"(?i)s",     "ſ")   -> match   (literal)
#
# `_SCRIPT_OPEN_RE` is a literal, so it matches `<ſcript`, `<scrİpt`, `<scrıpt`
# and `<ſtyle`. A fix that located the closer with a literal `(?i)</script>`,
# or that keyed a dict on `group(1).lower()`, therefore disagreed with the
# `</\1>` this file shipped with — it raised KeyError on the lookup (and
# `dashboard.py` turns that into a logged 202, losing the mail), and it ended
# spans early at a closer base did not accept, leaking element content into the
# stored body. Expected values below are what 6f62a57 produces.

@pytest.mark.parametrize("html,expected", [
    # KeyError shapes: the opener matches but `group(1).lower()` is no key.
    ("<p>hi</p><ſcript>SECRET=1</ſcript>", " hi\n "),
    ("<p>hi</p><scrİpt>SECRET=2</script>", " hi\n "),
    # Base does *not* accept `</script>` as the closer for `<scrıpt>` (U+0131
    # has no simple uppercase to `I`), so this content survives on base too.
    # Pinned as-is: the point is equivalence, not improvement.
    ("<p>hi</p><scrıpt>SECRET=3</script>", " hi\n SECRET=3 "),
    ("<p>hi</p><ſtyle>SECRET=4</style>", " hi\n SECRET=4 "),
    # An unclosed exotic opener needs no closer at all to reach the lookup.
    ("<p>hi</p><scrİpt SECRET=5", " hi\n<scrİpt SECRET=5"),
    # Span-end shapes: `(?i)</script>` matches `</ſcript>`, `</\1>` does not,
    # so an early stop leaks the rest of the element.
    ("<script>var s=1</ſcript>SECRET_TOKEN=9</script><p>hi</p>", " hi\n"),
    ("<style>a{}</ſtyle>SECRET_CSS</style><p>hi</p>", " hi\n"),
])
def test_exotic_case_folds_in_a_tag_name_match_base(html, expected):
    assert inbound.extract_body({"html": html}) == expected


def test_an_exotic_opener_does_not_raise_through_the_route(monkeypatch):
    """The KeyError was invisible: `dashboard.py` catches bare `Exception`,
    logs, and still answers 202, so the mail is accepted-and-lost with no
    user-visible signal. Assert the parse itself does not raise.
    """
    for name in ("ſcript", "scrİpt", "scrıpt", "ſtyle"):
        html = "<p>Message: hi</p><%s>x" % name
        inbound.extract_body({"html": html})  # must not raise


def test_many_distinct_opener_spellings_stay_linear():
    """The closer cache is keyed by `str.lower()`, which is not the engine's
    fold, so this pins the reason that is still safe: the opener alternation
    admits a fixed, finite set of spellings (64 for `script`), so the number of
    distinct keys is bounded by a constant and the scan stays linear. Base is
    quadratic on this shape (~4x per doubling, 2.5 s at n=8000).
    """
    variants = ["<" + "".join(c)
                for c in itertools.product(*[(ch.lower(), ch.upper())
                                             for ch in "script"])]
    assert len(variants) == 64

    def build(n):
        return "".join(variants[i % len(variants)] for i in range(n // 7))

    small = _best(inbound._strip_html, build(16_000))
    large = _best(inbound._strip_html, build(64_000))
    ratio = large / small if small else 0
    assert ratio < 8.0, (
        f"64 distinct opener spellings: 4x the input cost {ratio:.1f}x the "
        f"time — still superlinear ({small * 1000:.1f}ms -> "
        f"{large * 1000:.1f}ms)")


# --- the size cap ---------------------------------------------------------

def _post_chunked(app, raw, headers):
    """POST with no Content-Length, the way a chunked provider would.

    Driven straight at the WSGI app: Flask's test client re-derives the environ
    and drops `wsgi.input_terminated`, which silently delivers a zero-byte body
    and makes this look capped when it is really just blind.
    """
    import io
    from werkzeug.test import EnvironBuilder, run_wsgi_app

    environ = EnvironBuilder("/inbound/email", method="POST",
                             data=raw, headers=headers).get_environ()
    environ.pop("CONTENT_LENGTH", None)
    environ["wsgi.input"] = io.BytesIO(raw)
    environ["wsgi.input_terminated"] = True
    environ["HTTP_TRANSFER_ENCODING"] = "chunked"
    run_wsgi_app(app.wsgi_app, environ, buffered=True)


def test_size_cap_holds_without_a_content_length(monkeypatch):
    """`request.content_length or 0` turned "unknown" into "empty", and
    `if raw_size and raw_size > MAX` then skipped the check — so the same 1.9 MB
    body was rejected with the header and parsed without it."""
    import dashboard

    seen = {}
    original = ff_email.parse

    def spy(subject, body, received_at=""):
        seen["bodylen"] = len(body or "")
        return original(subject, body, received_at=received_at)

    monkeypatch.setattr(ff_email, "parse", spy)
    app = dashboard.app
    app.config["WTF_CSRF_ENABLED"] = False

    payload = {
        "recipient": inbound.address_for("7"),
        "from": "no-reply@furnishedfinder.com",
        "subject": "New lead from Jordan",
        # Deliberately *benign* filler rather than a pathological run: this test
        # is about the cap, not the cost. With `"a" * 1_900_000` the assertion
        # below still holds on the unfixed code, but only after the parser
        # grinds through 1.9 MB quadratically — which is the outage this ticket
        # is about, and it makes the test useless as a check of the cap.
        "text": "Traveler: Jordan Keller\nProperty: 123 Maple\n"
                "Move in: Mar 3, 2026\nMessage: "
                + ("the quick brown fox jumps over a lazy dog " * 46_000),
    }
    raw = json.dumps(payload).encode()
    assert len(raw) > inbound.MAX_PAYLOAD_BYTES
    headers = {"X-Inbound-Secret": "hook-secret", "Content-Type": "application/json"}

    # Positive control: with the header the cap already worked. If this ever
    # fails, the harness is broken rather than the cap.
    seen.clear()
    app.test_client().post("/inbound/email", data=raw, headers=headers)
    assert "bodylen" not in seen, "control: oversized body reached the parser"

    seen.clear()
    _post_chunked(app, raw, headers)
    assert "bodylen" not in seen, (
        f"chunked oversized body reached the parser with {seen.get('bodylen')} chars")


def test_size_cap_holds_for_a_form_encoded_chunked_body(monkeypatch):
    """A form-encoded body with no Content-Length must still be capped.

    Note what actually rejects it: the route reads `get_data()` *before* form
    parsing can drain the stream, so `accept` receives the real size and the
    route-level check fires. An earlier version of this docstring claimed the
    reordering did not help here and that `accept`'s body-length check was what
    caught it — instrumenting `accept` disproves that (it arrives with
    `raw_size=1800243`). That check is still worth keeping as the backstop for
    paths the caller genuinely cannot measure, but it is
    `test_size_cap_holds_when_content_length_is_zero_or_malformed` that covers
    it; without that test, deleting it left the suite fully green.
    """
    from urllib.parse import urlencode

    import dashboard

    seen = {}
    original = ff_email.parse

    def spy(subject, body, received_at=""):
        seen["bodylen"] = len(body or "")
        return original(subject, body, received_at=received_at)

    monkeypatch.setattr(ff_email, "parse", spy)
    app = dashboard.app
    app.config["WTF_CSRF_ENABLED"] = False

    raw = urlencode({
        "recipient": inbound.address_for("7"),
        "from": "no-reply@furnishedfinder.com",
        "subject": "New lead from Jordan",
        "secret": "hook-secret",
        "text": "Traveler: Jordan Keller\nProperty: 123 Maple\n"
                "Move in: Mar 3, 2026\nMessage: "
                + ("the quick brown fox jumps over a lazy dog " * 46_000),
    }).encode()
    assert len(raw) > inbound.MAX_PAYLOAD_BYTES

    _post_chunked(app, raw, {"Content-Type": "application/x-www-form-urlencoded"})
    assert "bodylen" not in seen, (
        f"form-encoded chunked body reached the parser with {seen.get('bodylen')} chars")


def test_size_cap_measures_the_request_not_just_the_extracted_body(monkeypatch):
    """A large *request* whose extracted body is small must still be capped.

    `len(body)` bounds one field, not the request, so it cannot see this: 1.8 MB
    on the wire with only ~400 KB of `text`. The request-level measurement has
    to happen before anything reads `request.form`, because form and multipart
    parsing drains the stream and leaves `get_data` empty — and form/multipart
    is what mail providers actually POST.
    """
    from urllib.parse import urlencode

    import dashboard

    seen = {}
    original = ff_email.parse

    def spy(subject, body, received_at=""):
        seen["bodylen"] = len(body or "")
        return original(subject, body, received_at=received_at)

    monkeypatch.setattr(ff_email, "parse", spy)
    app = dashboard.app
    app.config["WTF_CSRF_ENABLED"] = False

    raw = urlencode({
        "recipient": inbound.address_for("7"),
        "from": "no-reply@furnishedfinder.com",
        "subject": "New lead from Jordan",
        "secret": "hook-secret",
        "text": "Traveler: Jordan Keller\nProperty: 123 Maple\n"
                "Move in: Mar 3, 2026\nMessage: "
                + ("the quick brown fox " * 20_000),
        "pad": "z" * 1_400_000,
    }).encode()
    assert len(raw) > 1_500_000

    _post_chunked(app, raw, {"Content-Type": "application/x-www-form-urlencoded"})
    assert "bodylen" not in seen, (
        f"1.8 MB form request reached the parser (extracted {seen.get('bodylen')} chars)")


def test_a_normal_notification_is_still_accepted(monkeypatch):
    """The cap must not be the thing that starts dropping real mail."""
    import dashboard

    seen = {}
    original = ff_email.parse

    def spy(subject, body, received_at=""):
        seen["bodylen"] = len(body or "")
        return original(subject, body, received_at=received_at)

    monkeypatch.setattr(ff_email, "parse", spy)
    app = dashboard.app
    app.config["WTF_CSRF_ENABLED"] = False

    raw = json.dumps({
        "recipient": inbound.address_for("7"),
        "from": "no-reply@furnishedfinder.com",
        "subject": "New lead from Jordan",
        "text": "Traveler: Jordan Keller\nProperty: 123 Maple\nMove in: Mar 3, 2026\n",
    }).encode()
    headers = {"X-Inbound-Secret": "hook-secret", "Content-Type": "application/json"}

    app.test_client().post("/inbound/email", data=raw, headers=headers)
    assert seen.get("bodylen"), "a normal notification stopped reaching the parser"

    seen.clear()
    _post_chunked(app, raw, headers)
    assert seen.get("bodylen"), "a normal chunked notification was rejected"


@pytest.mark.parametrize("content_type", [
    "application/json",
    # What Mailgun, SendGrid Inbound Parse and Postmark actually POST. Reading
    # the body up front to measure it must not break form parsing below.
    "application/x-www-form-urlencoded",
])
@pytest.mark.parametrize("chunked", [False, True])
def test_real_mail_survives_every_transport(monkeypatch, content_type, chunked):
    from urllib.parse import urlencode

    import dashboard

    seen = {}
    original = ff_email.parse

    def spy(subject, body, received_at=""):
        seen["bodylen"] = len(body or "")
        return original(subject, body, received_at=received_at)

    monkeypatch.setattr(ff_email, "parse", spy)
    app = dashboard.app
    app.config["WTF_CSRF_ENABLED"] = False

    fields = {
        "recipient": inbound.address_for("7"),
        "from": "no-reply@furnishedfinder.com",
        "subject": "New lead from Jordan",
        "secret": "hook-secret",
        "text": "Traveler: Jordan Keller\nProperty: 123 Maple\nMove in: Mar 3, 2026\n",
    }
    raw = (json.dumps(fields) if content_type == "application/json"
           else urlencode(fields)).encode()
    headers = {"X-Inbound-Secret": "hook-secret", "Content-Type": content_type}

    if chunked:
        _post_chunked(app, raw, headers)
    else:
        app.test_client().post("/inbound/email", data=raw, headers=headers)
    assert seen.get("bodylen"), (
        f"a real notification was dropped on {content_type}"
        f"{' chunked' if chunked else ''}")


# --- defects found by adversarial review of the first cut of this fix -------
#
# Every test below failed against that first cut. They are here because the
# original suite passed it: making the parse path *fast* is only half the job,
# and nothing pinned that it still parsed the same things.

HTML_WITH_A_BARE_LT = (
    "<div><p>You have a new message on FurnishedFinder.</p>"
    "<p>Traveler: Jordan Keller</p><p>Property: 123 Maple St Unit B</p>"
    "<p>Message: Hi! My budget is &lt; $2000/mo but honestly < $2400 works. "
    "Can I reach you at jordan.keller@gmail.com?</p>"
    "<p>Thanks,<br>Jordan</p></div>"
)


def test_html_with_a_bare_less_than_extracts_exactly_what_base_did():
    """A `<` in a guest's prose must not change the extracted text.

    The first cut of this fix replaced `<[^>]+>` with `<[^<>]+>`, which ends a
    tag at the next `<` instead of the next `>`. On this input — a guest typing
    "< $2400", which is ordinary mail, not an attack — that extracted different
    text, changed `property_name`/`title`, and recovered an address base never
    saw. A differential corpus put it at 221 of 832 cases.

    That is not a safe "improvement". For `kind == "message"` the id hashes
    `_body_fingerprint(body)`, so changing the body changes the dedup key and
    every already-ingested message re-arrives as new on deploy — duplicate
    deals, duplicate auto-drafts. This ticket is about availability; it may not
    move parse output. Values pinned from `6f62a57`.

    (Base swallowing the address here *is* a real parsing bug — the `<` eats
    everything to the next `>`. It is a correctness change with a dedup
    migration attached, so it belongs in its own ticket, not this one.)
    """
    body = inbound.extract_body({"html": HTML_WITH_A_BARE_LT})
    assert body == (
        " You have a new message on FurnishedFinder.\n"
        " Traveler: Jordan Keller\n Property: 123 Maple St Unit B\n"
        " Message: Hi! My budget is &lt; $2000/mo but honestly Thanks,\nJordan\n\n")

    item = ff_email.parse("New message from Jordan", body)
    assert item["property_name"] == "123 Maple St Unit B"
    assert item["title"] == "123 Maple St Unit B | Jordan Keller"
    # `.get`, not `[...]`: base carries no `email` key at all for this item, and
    # the narrowed pattern made one appear. Asserting `item["email"] is None`
    # would raise KeyError rather than report the difference.
    assert item.get("email") is None
    assert item["id"] == "54bb38fb2a56937b"


@pytest.mark.parametrize("html,must_not_contain", [
    # `<script` with no word boundary: the old `<(script|style).*?</\1>` let
    # `.*?` absorb `y>SECRET=1;`, so it stripped this. Requiring a well-formed
    # tag leaked the element's content into the stored, rendered body.
    ("<p>hi</p><scripty>SECRET=1;</script>", "SECRET"),
    ("<p>hi</p><script foo=bar>SECRET=2;</script>", "SECRET"),
    ("<p>hi</p><STYLE x>SECRET=3;</STYLE>", "SECRET"),
])
def test_malformed_script_opener_content_is_still_removed(html, must_not_contain):
    assert must_not_contain not in inbound.extract_body({"html": html})


@pytest.mark.parametrize("shape,build", [
    ("script_runs", lambda n: "<script" * (n // 7)),
    ("style_runs", lambda n: "<style" * (n // 6)),
    ("mixed_runs", lambda n: "<script<style" * (n // 13)),
])
def test_unclosed_script_openers_stay_linear(shape, build):
    """The forward scan is only linear if a failed closer search is not repeated.

    Searching for `</script>` from each of n openers is O(n^2) even though each
    individual search is linear — the first cut of the rewrite scored 2.99x per
    doubling here, i.e. still quadratic, having merely moved the cost. Once a
    closer is absent from the rest of the input it is absent for every later
    opener, so the result is cached per kind.
    """
    small = _best(inbound._strip_html, build(16_000))
    large = _best(inbound._strip_html, build(64_000))
    ratio = large / small if small else 0
    assert ratio < 8.0, (
        f"{shape}: 4x the input cost {ratio:.1f}x the time — still superlinear "
        f"({small * 1000:.1f}ms -> {large * 1000:.1f}ms)")


def test_responder_email_scrape_stays_linear():
    """`responder._EMAIL_RE` is a fourth copy of the quadratic pattern.

    It is reached from `/inbound/email` via `runner.draft_ingested` ->
    `evaluate_lead` -> `_find_email`, which scans `item["title"]` — built from a
    whole unbounded `_label` line. So a body that is *legal* under the payload
    cap still yielded a ~400 KB title, and this took 604 s with the GIL held,
    five times the arbiter's 120 s timeout, while the three regexes the ticket
    named parsed the same body in 0.19 s. Fixing only the named sites left the
    outage reachable.
    """
    import responder

    small = _best(responder._find_email, {"title": "a" * 16_000})
    large = _best(responder._find_email, {"title": "a" * 64_000})
    ratio = large / small if small else 0
    assert ratio < 8.0, (
        f"4x the title cost {ratio:.1f}x the time — still superlinear "
        f"({small * 1000:.1f}ms -> {large * 1000:.1f}ms)")


@pytest.mark.parametrize("content_length", ["0", "bogus", "-5"])
def test_size_cap_holds_when_content_length_is_zero_or_malformed(monkeypatch,
                                                                content_length):
    """Werkzeug reports 0 for both `Content-Length: 0` and a malformed value.

    `if raw_size and raw_size > MAX` therefore read 0 as "unknown, skip the
    check" and let a sender opt out of the cap by lying about it. Deleting
    `accept`'s body-length check left the whole suite green while 2 MB reached
    the parser, so nothing covered this.
    """
    import dashboard

    seen = {}
    original = ff_email.parse

    def spy(subject, body, received_at=""):
        seen["bodylen"] = len(body or "")
        return original(subject, body, received_at=received_at)

    monkeypatch.setattr(ff_email, "parse", spy)
    app = dashboard.app
    app.config["WTF_CSRF_ENABLED"] = False

    raw = json.dumps({
        "recipient": inbound.address_for("7"),
        "from": "no-reply@furnishedfinder.com",
        "subject": "New lead from Jordan",
        "text": "Traveler: Jordan Keller\nProperty: 123 Maple\nMove in: Mar 3, 2026\n"
                "Message: " + ("the quick brown fox jumps over a lazy dog " * 48_000),
    }).encode()
    assert len(raw) > inbound.MAX_PAYLOAD_BYTES

    import io
    from werkzeug.test import EnvironBuilder, run_wsgi_app

    environ = EnvironBuilder(
        "/inbound/email", method="POST", data=raw,
        headers={"X-Inbound-Secret": "hook-secret",
                 "Content-Type": "application/json"}).get_environ()
    environ["CONTENT_LENGTH"] = content_length
    environ["wsgi.input"] = io.BytesIO(raw)
    environ["wsgi.input_terminated"] = True
    run_wsgi_app(app.wsgi_app, environ, buffered=True)

    assert "bodylen" not in seen, (
        f"Content-Length: {content_length} let a {len(raw)} byte body reach the "
        f"parser ({seen.get('bodylen')} chars)")
