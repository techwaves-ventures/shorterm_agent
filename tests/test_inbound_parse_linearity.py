"""VEN-144: the inbound parse path must stay linear, and the size cap must hold.

Regexes reachable from `/inbound/email` are O(n^2) in the length of an unbroken
whitespace-free run, and `re.search` holds the GIL for its whole duration.
`Procfile` runs `gunicorn --workers 1 --threads 8 --timeout 120`, so one message
of the right shape does not merely make one request slow — it freezes the only
worker, `/healthz` included, until the arbiter SIGKILLs it and drops every
in-flight request.

Nothing gates the cost: `ff_email.parse` calls `_dates(body)` *before* the "is
this a real notification" check below it, so the only precondition is a
non-empty body that got past the provider secret.

Measured on `0f0100e` (min-of-3 CPU seconds, 8x lever, so ~8 is linear and ~64
is quadratic):

    shape                base    patched
    _dates letters      63.8x      7.2x     4.92s -> 0.004s at n=16000
    _EMAIL_RE letters   62.2x      7.8x
    _EMAIL_RE digits    65.8x      4.9x
    _EMAIL_RE base64    67.2x      8.1x
    _EMAIL_RE dots      65.2x      6.6x
    responder title     62.9x      8.3x
    parse end to end    62.5x      9.3x     5.67s -> 0.012s at n=16000

Two things this file deliberately does *not* cover, because `main` already
does:

* The HTML strip. VEN-152 replaced the `<[^>]+>` substitution chain with an
  `html.parser` extractor, and `tests/test_inbound_html_bare_lt.py` carries the
  linearity guards for it. An earlier cut of this ticket hand-rolled a linear
  substitution instead; porting that forward would revert VEN-152.
* `sites/furnishedfinder._EMAIL_RE`, the scrape-path copy, which VEN-151
  already fixed the same way — see
  `test_ff_connect_flow.test_lead_detail_email_regex_avoids_quadratic_rescans`.

The equality tests below pin behaviour that already holds on `0f0100e`; they
exist so a *faster* regex that quietly matches a different substring cannot
pass. Both the stated dates and the extracted address feed the item id, so
changing what is matched splits one lead into two or collapses two into one —
a perf fix that alters parse output is a data bug.
"""
import io
import json
import os
import tempfile
import time
from urllib.parse import urlencode

import pytest
from werkzeug.test import EnvironBuilder, run_wsgi_app

os.environ.setdefault("SQLITE_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("INBOUND_EMAIL_DOMAIN", "inbound.example.com")
os.environ.setdefault("INBOUND_WEBHOOK_SECRET", "hook-secret")

import inbound  # noqa: E402
import responder  # noqa: E402
from sites import ff_email  # noqa: E402

# A body that genuinely parses, so cost is measured on the path a real
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


# --- linearity ------------------------------------------------------------
#
# Method is VEN-164's, copied deliberately rather than imported: CPU time
# rather than wall time, minimum rather than median, and an 8x lever rather
# than a 2x one. See the note above `_LINEAR_RATIO_CEILING` in
# `tests/test_inbound_html_bare_lt.py` for why each of those matters — in
# short, a `perf_counter` median over a 2x lever put its ceiling 5% under its
# own positive control and flaked on a loaded runner while the implementation
# was perfectly linear.
#
# The ceiling is the same 16.0, and it is justified by the same margins for
# these shapes: every patched reading above is 4.9-9.3, and every base reading
# is 62.2-67.2. So 16.0 sits a factor of 1.7 above the slowest linear reading
# and a factor of 3.9 below the fastest quadratic one.
_LINEAR_RATIO_CEILING = 16.0

# Below this the measurement is noise, not signal. It fires only if the payload
# stopped reaching the code under test at all — which is the failure mode that
# makes a "passing" ratio meaningless.
_MEASURABLE_SECONDS = 1e-6


def _growth_ratio(call, make, lo=2000, hi=16000, runs=3):
    """Fastest-of-`runs` CPU seconds at `lo` and `hi`. ~8 is linear, ~64 quadratic."""
    timings = {}
    for n in (lo, hi):
        payload = make(n)
        best = None
        for _ in range(runs):
            start = time.process_time()
            call(payload)
            elapsed = time.process_time() - start
            best = elapsed if best is None else min(best, elapsed)
        timings[n] = best
    assert timings[lo] > _MEASURABLE_SECONDS, (
        f"baseline at n={lo} was {timings[lo]:.2e}s, too small to build a ratio "
        "on; this guard is not measuring the parser any more")
    return timings[hi] / timings[lo]


def _assert_stays_linear(call, make, shape, attempts=3):
    """Re-measure before failing. A ratio is a sample, and one bad sample on a
    contended runner should not condemn the implementation.

    This cannot rescue a genuinely quadratic parser: base comes back at 62-67
    on every attempt, nowhere near the 16.0 ceiling.
    """
    seen = []
    for _ in range(attempts):
        ratio = _growth_ratio(call, make)
        seen.append(ratio)
        if ratio < _LINEAR_RATIO_CEILING:
            return
    raise AssertionError(
        f"{shape} scaling looks quadratic: {', '.join(f'{r:.2f}x' for r in seen)} "
        f"per 8x of input over {attempts} attempts, ceiling {_LINEAR_RATIO_CEILING}")


# Runs that drove a quantifier to end-of-input from every offset inside them.
# `dashes`, `dots` and `plus` are not decoration: `-`, `.` and `+` are all
# inside `_EMAIL_RE`'s character class, and they are the shapes that survive a
# narrower lookbehind — see `test_email_lookbehind_covers_the_whole_class`.
RUNS = {
    "letters": lambda n: "a" * n,
    "caps": lambda n: "A" * n,
    "digits": lambda n: "7" * n,
    "base64": lambda n: ("QWxhZGRpbjpvcGVuIHNlc2FtZQ" * (n // 26 + 1))[:n],
    "dashes": lambda n: "-" * n,
    "dots": lambda n: "." * n,
    "plus": lambda n: "+" * n,
    "word_chars": lambda n: ("Ab3_" * (n // 4 + 1))[:n],
    "at_runs": lambda n: (("a" * 60 + "@") * (n // 61 + 1))[:n],
}


@pytest.mark.parametrize("shape", sorted(RUNS))
def test_date_scan_stays_linear(shape):
    """`_dates` runs on every body that gets past the provider secret, before
    any check that the message is a notification at all. Base: 4.9s of CPU on a
    16 KB letter run, 63.8x per 8x of input."""
    _assert_stays_linear(ff_email._dates, RUNS[shape], f"_dates/{shape}")


@pytest.mark.parametrize("shape", sorted(RUNS))
def test_email_scan_stays_linear(shape):
    """`_EMAIL_RE` re-scanned an unbroken run from every offset inside it before
    concluding there was no `@`."""
    _assert_stays_linear(ff_email._EMAIL_RE.search, RUNS[shape],
                         f"_EMAIL_RE/{shape}")


@pytest.mark.parametrize("shape", sorted(RUNS))
def test_responder_email_scrape_stays_linear(shape):
    """`responder._EMAIL_RE` is another copy of the same pattern, reached from
    the same request one layer further along: `runner.draft_ingested` ->
    `evaluate_lead` -> `_find_email`, which scans `item["title"]` — and
    `ff_email.parse` builds that title out of a whole unbounded `_label` line.
    So a body that is *legal* under the payload cap still produces a title
    hundreds of kilobytes long. Fixing only the regexes the ticket named would
    have left the outage reachable behind them."""
    _assert_stays_linear(lambda t: responder._find_email({"title": t}),
                         RUNS[shape], f"responder/{shape}")


def test_whole_parse_stays_linear_on_a_body_that_reaches_every_site():
    """End to end through the public entry point, on a body that clears the
    notification gate *and* carries a long unbroken run — so `_dates`, the
    labels and `_EMAIL_RE` are all reached. A bare run bails at the gate, which
    is how the original investigation came to report `_dates` as the sole hot
    spot when there were four sites."""
    _assert_stays_linear(
        lambda b: ff_email.parse("New lead from Emma M.", b),
        lambda n: FF_BODY.format(payload="a" * n),
        "parse end to end")


def test_ordinary_prose_was_never_the_problem():
    """The blowup needs one unbroken whitespace-free token. Ordinary mail — and
    `text/plain` wrapped at ~78 columns in particular — has one every few
    characters, which is why this never showed up in normal traffic. Pinned so
    a future 'just reject long bodies' guard cannot be sold as the fix."""
    prose = "the quick brown fox jumps over a lazy dog " * 12_000  # ~500 KB
    start = time.process_time()
    ff_email.parse("New lead from Emma M.", FF_BODY.format(payload=prose))
    assert time.process_time() - start < 2.0


def test_email_lookbehind_covers_the_whole_class():
    """`(?<![\\w])` instead of `(?<![\\w.+-])` produces *identical* output.

    Verified by fuzzing 400,000 strings over `ab.@+-_09 \\nİ` against the real
    pattern: zero divergences. `search` returns the leftmost match, and a match
    that starts mid-token implies one starting at that token's start because
    the greedy `[\\w.+-]+` absorbs the prefix — so no output test, and no
    differential corpus, can ever distinguish the two.

    It is still quadratic, because `.`, `+` and `-` are not `\\w`: on a 16 KB
    run of dots it measures 63.2x per 8x of input against the correct
    pattern's 3.9x. This is the one assertion in the file that has to read the
    pattern itself, and the reason it exists.
    """
    for pattern in (ff_email._EMAIL_RE.pattern, responder._EMAIL_RE.pattern):
        assert pattern.startswith(r"(?<![\w.+-])"), (
            "the lookbehind must veto every character the local-part class can "
            f"match, not just word characters: {pattern}")


# --- behaviour that must survive the change -------------------------------

# `_dates` reports what the mail *stated*, normalized but not validated, so
# these are m/d/yy rather than ISO.
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
    # group it also guards this branch, whose leading digit has no
    # absorb-the-prefix property. That mutant diverges on 264 of 5840 corpus
    # cases — it truncates the first to "2/1/26" and drops the third entirely.
    ("ref12/1/2026 through 6/30/2027", ("12/1/26", "6/30/27")),
    ("abc12/1/26 - 3/4/27", ("12/1/26", "3/4/27")),
    ("x9/1/26 to 12/31/26", ("9/1/26", "12/31/26")),
])
def test_digit_dates_are_not_truncated_by_the_lookbehind(text, expected):
    assert ff_email._dates(text) == expected


@pytest.mark.parametrize("text,expected", [
    # A word separator running straight into the second month. Only the *first*
    # group may take a lookbehind: the second is matched at a fixed point after
    # the separator rather than scanned for, so a lookbehind there sees the
    # separator's own last letter ("to|Jun") and vetoes the entire range —
    # `_dates` returns ("", "") and the lead loses both dates, or is dropped
    # outright when the notification has no property line to fall back on.
    # 400 of 5840 corpus cases diverge on that mutant.
    ("Jan 5, 2026 toJun 9, 2026", ("1/5/26", "6/9/26")),
    ("Jan 5, 2026 throughMar 1, 2027", ("1/5/26", "3/1/27")),
    ("Jan 5, 2026 untilMar 1, 2027", ("1/5/26", "3/1/27")),
    (" 9/9/26toMar. 31, 2026", ("9/9/26", "3/31/26")),
])
def test_word_separator_abutting_the_month_still_matches(text, expected):
    assert ff_email._dates(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("Septemberish 3, 2026 - Jun 9, 2026", ("9/3/26", "6/9/26")),
    ("Mayonnaise 3, 2026 to Jun 9, 2026", ("5/3/26", "6/9/26")),
])
def test_an_over_long_month_word_is_not_re_entered_mid_word(text, expected):
    """Capping the month tail (`[a-z]{0,7}`) instead of anchoring its start
    looks equivalent and is not: on "Septemberish" it re-enters the word and
    reports the tail. 89 of 5840 corpus cases diverge."""
    assert ff_email._dates(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("Contact: jordan.keller@gmail.com", "jordan.keller@gmail.com"),
    ("j+ff@sub.example.co.uk", "j+ff@sub.example.co.uk"),
    ("first_last@company-name.com", "first_last@company-name.com"),
    ("name.with.many.dots@example.museum", "name.with.many.dots@example.museum"),
    ("user+tag+more@mail.example.org", "user+tag+more@mail.example.org"),
    ("Reach me at jane@doe.net or call", "jane@doe.net"),
    ("meet @ 5pm, no address here", None),
    # Non-ASCII either side of the address. `\w` is Unicode-aware, so a
    # lookbehind over it has to be reasoned about in the same alphabet the
    # class matches — `İ` (U+0130) is the codepoint that broke an earlier cut
    # of this work, because `str.lower()` turns it into *two* characters.
    ("İlkay yazdı: ilkay@example.com", "ilkay@example.com"),
    ("renée@exämple.com yazdı", "renée@exämple.com"),
    ("中文 guest@example.com 中文", "guest@example.com"),
    # The two cases that killed the bounded-quantifier attempt: `{1,64}` on the
    # local part matched a truncated *tail* of an over-long address, which
    # would store the wrong address on the lead, and a bound on the domain
    # dropped long labels outright. 120 of 5840 corpus cases diverge.
    ("x" * 70 + "@example.com", "x" * 70 + "@example.com"),
    ("kim@" + "d" * 70 + ".com", "kim@" + "d" * 70 + ".com"),
    # A run of class characters immediately before a real address: the exact
    # shape the lookbehind prunes, and it must still find the whole thing.
    ("." * 2000 + "jane@doe.net", "." * 2000 + "jane@doe.net"),
    ("notes:\n" + "a" * 2500 + "\nEmail:\nlead+tag@example-domain.com",
     "lead+tag@example-domain.com"),
])
def test_email_addresses_are_found_whole(text, expected):
    for module in (ff_email, responder):
        m = module._EMAIL_RE.search(text)
        assert (m.group(0) if m else None) == expected, module.__name__


def test_item_id_is_unchanged():
    """The stated dates and the extracted address feed the item id, so a regex
    that matches a different substring silently splits one lead into two. This
    id was read off `0f0100e` before the change."""
    item = ff_email.parse("New lead from Jordan", FF_BODY.format(
        payload="I am a travel nurse. Reach me at jordan.keller@gmail.com"))
    assert item["move_in"] == "3/3/26"
    assert item["move_out"] == "6/30/26"
    assert item["email"] == "jordan.keller@gmail.com"
    assert item["id"] == "97af61eecfd80071"


def test_message_item_id_is_unchanged():
    """A `message` mixes `_body_fingerprint(body)` into its id, so it is the
    kind that detects a parse-output change the hardest — a lead's id would
    survive a body that parsed slightly differently. Pinned off `0f0100e`.

    The subject spells the name in ASCII on purpose: `_guest_name` matches
    `[A-Z]`, which `İ` is not, so an `İ` there makes `parse` return None and
    the assertion below would never run. The `İ` that matters is the one in
    the *body*, which is what the fingerprint hashes.
    """
    item = ff_email.parse(
        "New message from Ilkay D.",
        "İlkay D. wrote:\nMove in: Mar 3, 2026\nReach me at ilkay@example.com\n",
        received_at="Mon, 1 Jun 2026 09:00:00 +0000")
    assert item["kind"] == "message"
    assert item["id"] == "7f3ee5575e43ecf6"




# --- the size cap ---------------------------------------------------------
#
# Every test below pins its own `INBOUND_*` environment rather than trusting
# the `setdefault` at the top of this file. pytest imports *all* test modules
# during collection, and `tests/test_inbound_rejects.py` assigns
# `INBOUND_WEBHOOK_SECRET = "provider-secret"` at module scope — so by the time
# these run, the secret this file set no longer applies. Run alone the tests
# passed; run with the suite they got a 202 for `bad webhook secret`.
#
# That failure mode is worse than order-dependence, and it is why
# `_assert_cap_holds` insists on a positive control on the *same* transport:
# every assertion here is of the form "the body did not reach the parser", and
# a wrong secret satisfies all of them. A cap test that a rejected request
# passes is not testing the cap.

WEBHOOK_SECRET = "hook-secret"
INBOUND_DOMAIN = "inbound.example.com"


@pytest.fixture(autouse=True)
def _pinned_inbound_env(monkeypatch):
    monkeypatch.setenv("INBOUND_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("INBOUND_EMAIL_DOMAIN", INBOUND_DOMAIN)


def _spy_on_parse(monkeypatch):
    """Record the body length `ff_email.parse` was called with, if at all."""
    seen = {}
    original = ff_email.parse

    def spy(subject, body, received_at=""):
        seen["bodylen"] = len(body or "")
        return original(subject, body, received_at=received_at)

    monkeypatch.setattr(ff_email, "parse", spy)
    return seen


def _app():
    import dashboard

    dashboard.app.config["WTF_CSRF_ENABLED"] = False
    return dashboard.app


def _post(app, raw, headers, chunked, content_length=None):
    """POST the bytes, optionally the way a chunked provider would.

    The chunked arm is driven straight at the WSGI app: Flask's test client
    re-derives the environ and drops `wsgi.input_terminated`, which silently
    delivers a zero-byte body and makes this look capped when it is only blind.
    """
    if not chunked and content_length is None:
        app.test_client().post("/inbound/email", data=raw, headers=headers)
        return
    environ = EnvironBuilder("/inbound/email", method="POST",
                             data=raw, headers=headers).get_environ()
    if content_length is None:
        environ.pop("CONTENT_LENGTH", None)
        environ["HTTP_TRANSFER_ENCODING"] = "chunked"
    else:
        environ["CONTENT_LENGTH"] = content_length
    environ["wsgi.input"] = io.BytesIO(raw)
    environ["wsgi.input_terminated"] = True
    run_wsgi_app(app.wsgi_app, environ, buffered=True)


def _fields(text, **extra):
    fields = {
        "recipient": inbound.address_for("7"),
        "from": "no-reply@furnishedfinder.com",
        "subject": "New lead from Jordan",
        "secret": WEBHOOK_SECRET,
        "text": "Traveler: Jordan Keller\nProperty: 123 Maple\n"
                "Move in: Mar 3, 2026\n" + text,
    }
    fields.update(extra)
    return fields


def _encode(fields, content_type):
    if content_type == "application/json":
        return json.dumps(fields).encode()
    return urlencode(fields).encode()


# Benign filler rather than a pathological run: these are tests of the cap, not
# of the cost. With `"a" * 1_900_000` the assertions still hold on unfixed
# code, but only after the parser grinds through 1.9 MB quadratically first.
_FILLER = "the quick brown fox jumps over a lazy dog "


def _assert_cap_holds(monkeypatch, raw, headers, chunked, content_length,
                      why, content_type="application/json"):
    """Assert an oversized request never reaches the parser — and that an
    ordinary one on the identical transport still does.

    Without the second half this asserts nothing: "did not reach the parser" is
    also what a bad secret, an unknown recipient and a broken harness produce.
    """
    app = _app()

    control = _spy_on_parse(monkeypatch)
    _post(app, _encode(_fields("Message: hello\n"), content_type), headers,
          chunked, content_length=content_length)
    assert control.get("bodylen"), (
        f"control: an ordinary notification did not reach the parser on this "
        f"transport either, so {why} proves nothing")

    seen = _spy_on_parse(monkeypatch)
    _post(app, raw, headers, chunked, content_length=content_length)
    assert "bodylen" not in seen, (
        f"{why}: a {len(raw)} byte request reached the parser "
        f"({seen.get('bodylen')} chars)")


def test_size_cap_holds_without_a_content_length(monkeypatch):
    """`request.content_length or 0` turned "unknown" into "empty", and
    `if raw_size and raw_size > MAX` then skipped the check — so the same
    1.9 MB body was rejected with the header and parsed without it."""
    raw = _encode(_fields("Message: " + _FILLER * 46_000), "application/json")
    assert len(raw) > inbound.MAX_PAYLOAD_BYTES
    headers = {"X-Inbound-Secret": WEBHOOK_SECRET,
               "Content-Type": "application/json"}

    # With the header the cap already worked on base; assert that first, so a
    # failure below is about the missing header rather than about the cap.
    seen = _spy_on_parse(monkeypatch)
    _post(_app(), raw, headers, chunked=False)
    assert "bodylen" not in seen, "control: oversized body reached the parser"

    _assert_cap_holds(monkeypatch, raw, headers, chunked=True,
                      content_length=None, why="chunked oversized body")


def test_size_cap_holds_for_a_form_encoded_chunked_body(monkeypatch):
    """A form-encoded body with no Content-Length must still be capped.

    This is the transport that matters: Mailgun, SendGrid Inbound Parse and
    Postmark all POST form data. What rejects it is the route reading
    `get_data(cache=True)` *before* form parsing can drain the stream, so
    `accept` receives a real size — instrumenting `accept` shows it arriving
    with `raw_size=1800243` rather than 0.
    """
    ctype = "application/x-www-form-urlencoded"
    raw = _encode(_fields("Message: " + _FILLER * 46_000), ctype)
    assert len(raw) > inbound.MAX_PAYLOAD_BYTES

    _assert_cap_holds(monkeypatch, raw, {"Content-Type": ctype}, chunked=True,
                      content_length=None, why="form-encoded chunked body",
                      content_type=ctype)


def test_size_cap_measures_the_request_not_just_the_extracted_body(monkeypatch):
    """A large *request* whose extracted body is small must still be capped.

    `len(body)` bounds one field, not the request, so the backstop in `accept`
    cannot see this: 1.8 MB on the wire with only ~400 KB of `text`. It is what
    stops that backstop from being sold as the whole fix.
    """
    ctype = "application/x-www-form-urlencoded"
    raw = _encode(_fields("Message: " + "the quick brown fox " * 20_000,
                          pad="z" * 1_400_000), ctype)
    assert len(raw) > 1_500_000

    _assert_cap_holds(monkeypatch, raw, {"Content-Type": ctype}, chunked=True,
                      content_length=None,
                      why="1.8 MB form request with a small text field",
                      content_type=ctype)


@pytest.mark.parametrize("content_length", ["0", "bogus", "-5"])
def test_size_cap_holds_when_content_length_is_zero_or_malformed(
        monkeypatch, content_length):
    """Werkzeug reports 0 for `Content-Length: 0` and for a malformed value
    alike, so `if raw_size and raw_size > MAX` read 0 as "unknown, skip the
    check" and let a sender opt out of the cap by lying about it.

    This is the only test covering `accept`'s body-length backstop: deleting
    that check leaves every other test in this file green.
    """
    raw = _encode(_fields("Message: " + _FILLER * 46_000), "application/json")
    assert len(raw) > inbound.MAX_PAYLOAD_BYTES

    _assert_cap_holds(monkeypatch, raw,
                      {"X-Inbound-Secret": WEBHOOK_SECRET,
                       "Content-Type": "application/json"},
                      chunked=False, content_length=content_length,
                      why=f"Content-Length: {content_length}")


@pytest.mark.parametrize("content_type", [
    "application/json",
    "application/x-www-form-urlencoded",
])
@pytest.mark.parametrize("chunked", [False, True])
def test_real_mail_survives_every_transport(monkeypatch, content_type, chunked):
    """The cap must not be the thing that starts dropping real mail, and
    reading the body up front to measure it must not break form parsing."""
    seen = _spy_on_parse(monkeypatch)
    raw = _encode(_fields("Message: hello\n"), content_type)
    headers = {"X-Inbound-Secret": WEBHOOK_SECRET, "Content-Type": content_type}

    _post(_app(), raw, headers, chunked=chunked)
    assert seen.get("bodylen"), (
        f"a real notification was dropped on {content_type}"
        f"{' chunked' if chunked else ''}")
