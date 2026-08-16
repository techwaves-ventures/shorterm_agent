"""VEN-152 — a bare `<` in guest prose must not swallow the rest of the email.

`inbound.extract_body`'s HTML fallback stripped tags with `<[^>]+>`, so a guest
writing "my budget is < $2400" had everything up to the next `>` deleted — the
rest of their message plus several real tags. That routinely took the guest's
reply address with it. Only the HTML fallback is affected; providers that send a
text part were never touched.

Two things about how these tests are written, both learned the hard way here.

**They assert at `ff_email.parse` level, not at `extract_body` level.** The
defects that made the obvious fixes unshippable — a comment's innards leaking
onto the `Traveler:` line so `parse` returns None and the enquiry is dropped —
are entirely invisible in the extracted string. A test that only compares body
text calls those fixes green.

**The ticket's own repro snippet is degenerate.** In isolation it carries no
`Traveler:` line, so `parse` returns None on the fixed head *and* the broken
one; asserting on it proves nothing about the item. `test_the_bare_less_than_*`
below therefore embeds the identical prose in a real notification, which is the
form that actually discriminates.
"""
import time

import pytest

import inbound
from sites import ff_email

MESSAGE_SUBJECT = "You have a new message from your traveler"
LEAD_SUBJECT = "You have a new tenant lead"

# The table every FurnishedFinder notification carries. `parse` needs the
# Traveler row to produce an item at all.
TABLE = ("<table>"
         "<tr><td>Property</td><td>Sunny 1BR</td></tr>"
         "<tr><td>Traveler</td><td>Emma M.</td></tr>"
         "<tr><td>Date received</td><td>8/14/26</td></tr>"
         "</table>")


def notification(inner: str, prefix: str = "") -> str:
    return ("<html><body>" + prefix +
            "<p>You have a new message from your traveler.</p>" +
            TABLE + inner + "</body></html>")


def notification_with_markup_at(markup: str) -> str:
    """The same notification with `markup` wedged immediately before the Traveler
    row — the placement that actually discriminates.

    Mutation testing found this: with the markup at the top of the document a
    leak lands on a harmless line and every drop assertion passes even for the
    rejected `<[a-zA-Z!/][^<>]*>` candidate. Adjacent to the Traveler cell, the
    same leak un-anchors `_guest_name` and `parse` returns None. Placement, not
    the markup, is what makes this test able to fail.
    """
    return ("<html><body><p>You have a new message from your traveler.</p><table>"
            "<tr><td>Property</td><td>Sunny 1BR</td></tr>" + markup +
            "<tr><td>Traveler</td><td>Emma M.</td></tr>"
            "<tr><td>Date received</td><td>8/14/26</td></tr>"
            "</table><p>Is the unit still available?</p></body></html>")


def test_the_bare_less_than_no_longer_eats_the_guests_address_and_ask():
    """The ticket's repro, in the form that reaches `parse` as a real item.

    On `6f62a57` the extracted body ended at "...but honestly" and resumed at
    "Thanks," — the budget, the question and the address were gone, and
    `item["email"]` was absent.
    """
    html = notification(
        "<p>Message: Hi! My budget is &lt; $2000/mo but honestly < $2400 works. "
        "Can I reach you at jordan.keller@gmail.com?</p>"
        "<p>Thanks,<br>Jordan</p>")
    item = ff_email.parse(MESSAGE_SUBJECT, inbound.extract_body({"HtmlBody": html}))

    assert item is not None
    assert item["email"] == "jordan.keller@gmail.com", "the reply address is the point"
    assert "$2400 works" in item["body"], "the guest's actual ask must survive"
    assert "Can I reach you" in item["body"]


def test_a_bare_less_than_with_a_later_greater_than_survives():
    """Kills the `<[^<>]+>` candidate specifically.

    "End the tag at the next `<` too" fixes only the case where no `>` follows.
    Here one does, so that candidate deletes "< 10 >" exactly as base did and
    this test stays red for it.
    """
    body = inbound.extract_body({"HtmlBody": "<p>5 < 10 > 3</p>"})
    assert "5 < 10 > 3" in body


@pytest.mark.parametrize("label,prefix", [
    # Outlook emits a conditional comment in nearly every HTML mail it sends,
    # so this is the common case rather than an exotic one.
    ("outlook conditional", "<!--[if !mso]><!--><span></span><!--<![endif]-->"),
    ("comment containing <", "<!-- a < b -->"),
    ("processing instruction", "<?xml version=\"1.0\"?>"),
    ("cdata containing <", "<![CDATA[ if (a<b) x ]]>"),
    ("doctype", "<!DOCTYPE html>"),
    # Content-free on purpose. An anchor with visible text puts real words next
    # to the Traveler cell, which un-anchors `_guest_name` on *every* head
    # including base — a pre-existing `sites.ff_email` fragility, not this
    # extractor's business. Empty elements isolate leaked *markup* from
    # legitimate *text*, which is what these cases are actually about.
    ("attribute containing >", "<a href=\"x\" title=\"a > b\"></a>"),
    ("attribute containing <", "<a href=\"x\" title=\"a < b\"></a>"),
    ("img alt containing >", "<img src=\"x\" alt=\"a > b\">"),
])
def test_markup_grammars_never_drop_the_enquiry(label, prefix):
    """The critical one: no candidate fix may turn an enquiry into nothing.

    A stripper that leaks a comment's innards pushes text onto the `Traveler:`
    line, `_guest_name` stops finding a name, `parse` returns None and
    `inbound.accept` raises `Rejected`. The webhook still answers 202, so the
    provider never retries — the enquiry is destroyed with no trace. This killed
    the `<[a-zA-Z!/][^<>]*>` candidate, which passed every other check.
    """
    for placement, html in (
            ("at the top of the document",
             notification("<p>Is the unit still available?</p>", prefix=prefix)),
            ("immediately before the Traveler cell",
             notification_with_markup_at(prefix)),
    ):
        item = ff_email.parse(MESSAGE_SUBJECT, inbound.extract_body({"HtmlBody": html}))

        where = f"{label} {placement}"
        assert item is not None, f"{where}: the enquiry was dropped entirely"
        assert item["sender"] == "Emma M.", f"{where}: guest name corrupted"
        assert "still available" in item["body"], f"{where}: the guest's words were lost"


@pytest.mark.parametrize("label,prefix", [
    ("outlook conditional", "<!--[if !mso]><!--><span></span><!--<![endif]-->"),
    ("comment containing <", "<!-- a < b -->"),
    ("processing instruction", "<?xml version=\"1.0\"?>"),
    ("cdata containing <", "<![CDATA[ if (a<b) x ]]>"),
    ("doctype", "<!DOCTYPE html>"),
])
def test_no_markup_innards_leak_into_a_stored_field(label, prefix):
    """A leak is worse than the bug: it is shown to the host as the guest's words.

    VEN-127's `_BOILERPLATE` anchoring is un-anchored by a leaked prefix, and the
    thread view then displays the guest "saying" the template's own header line.
    """
    html = notification("<p>Is the unit still available?</p>", prefix=prefix)
    item = ff_email.parse(MESSAGE_SUBJECT, inbound.extract_body({"HtmlBody": html}))
    assert item is not None

    for field in ("sender", "title", "body"):
        value = item.get(field) or ""
        for token in ("<!", "<?", "CDATA", "[if ", "endif"):
            assert token not in value, f"{label}: {token!r} leaked into {field}: {value[:80]!r}"


@pytest.mark.parametrize("markup", [
    "<a href=\"x\" title=\"a > b\"></a>",
    "<img src=\"x\" alt=\"Save > 20%\">",
])
def test_an_attribute_containing_a_greater_than_no_longer_destroys_the_enquiry(markup):
    """A second total-ingestion-loss bug on base, found while testing this fix.

    `<[^>]+>` ends the tag at the first `>`, which for `alt="Save > 20%"` is
    *inside the attribute*, so the remainder (`20%">`) is emitted as text. Landing
    next to the Traveler cell it un-anchors the name and `parse` returns None —
    the enquiry is destroyed and the webhook still answers 202. A parser reads
    the quoted value properly, so this class disappears with the same change.

    Not in the ticket; recorded here because it shares the fix and would
    otherwise look like an unexplained id drift in the differential corpus.
    """
    item = ff_email.parse(
        MESSAGE_SUBJECT,
        inbound.extract_body({"HtmlBody": notification_with_markup_at(markup)}))

    assert item is not None, "base drops this enquiry outright"
    assert item["sender"] == "Emma M."


def test_script_and_style_bodies_still_never_reach_the_guest_text():
    html = notification("<p>Hello there</p>",
                        prefix="<style>.a{color:red}</style>"
                               "<script>if (a<b) { alert(1); }</script>")
    item = ff_email.parse(MESSAGE_SUBJECT, inbound.extract_body({"HtmlBody": html}))

    assert item is not None
    assert "alert" not in item["body"] and "color:red" not in item["body"]
    assert "Hello there" in item["body"]


def test_entities_are_not_decoded():
    """Deliberate: the old stripper never decoded them and the money/email
    patterns downstream are written against the un-decoded text. Decoding would
    also silently move the dedup id of every message containing an entity."""
    body = inbound.extract_body(
        {"HtmlBody": "<p>Budget is &lt; $2000 &amp; flexible</p>"})
    assert "&lt;" in body and "&amp;" in body
    assert "< $2000" not in body


def test_a_text_part_still_wins_and_is_returned_untouched():
    """The HTML path is a fallback. A provider that sends text must be bit-stable."""
    raw = "Traveler: Emma M.\nbudget < 2400 > firm\n"
    assert inbound.extract_body({"text": raw, "HtmlBody": "<p>ignored</p>"}) == raw


def test_the_html_fallback_layout_still_finds_the_guest():
    """Guards the collapse-runs-to-one-space contract `test_review_fixes_3` relies on."""
    body = inbound.extract_body({"HtmlBody": notification("<p>Hi! Is it available?</p>")})
    assert " Traveler Emma M. " in body


@pytest.mark.parametrize("label,markup", [
    # Every one of these was a *regression introduced by the first version of
    # this fix*: html.parser reports an unterminated construct by handing over
    # the whole rest of the document, and dropping that blob (the obvious
    # reading of "script bodies are not prose") destroyed the enquiry. An
    # unclosed <style> in a template head is ordinary, not exotic.
    ("unclosed <style> in head", "<style type=\"text/css\">.btn{color:#fff}"),
    ("unclosed <script> in head", "<script>var t=1;"),
    ("unterminated comment", "<!--x"),
    ("unterminated CDATA", "<![CDATA[ x"),
    ("MSO conditional missing its >", "<!--[if gte mso 9]><xml></xml><![endif]--"),
])
def test_an_unterminated_construct_never_swallows_the_document(label, markup):
    html = ("<html><head>" + markup + "</head><body>" + TABLE +
            "<p>Is the unit still available? Reach me at emma.r@gmail.com</p>"
            "</body></html>")
    item = ff_email.parse(MESSAGE_SUBJECT, inbound.extract_body({"HtmlBody": html}))

    assert item is not None, f"{label}: the rest of the email was discarded"
    assert item["sender"] == "Emma M."
    assert item["email"] == "emma.r@gmail.com", f"{label}: the address was lost"
    for token in ("<!", "<?", "CDATA[", "[if "):
        assert token not in (item.get("body") or ""), f"{label}: {token!r} leaked"


def test_an_unclosed_opaque_element_releases_its_content_at_eof():
    """Asserted on the extractor directly, deliberately.

    Mutation testing showed the end-to-end tests above cannot see this: break the
    flush and `strip_html`'s empty-output fallback quietly rescues the result, so
    every test stays green while the real mechanism is dead. Two layers of
    defence means each one needs its own test.
    """
    extractor = inbound._BodyExtractor()
    extractor.feed("<p>before</p><style>.a{color:red}")
    text = extractor.finish()

    assert "before" in text
    assert ".a{color:red}" in text, "content after an unclosed <style> was dropped"


def test_a_properly_closed_opaque_element_still_discards_its_content():
    """The other half of the pair — the flush must not resurrect real script."""
    extractor = inbound._BodyExtractor()
    extractor.feed("<p>before</p><style>.a{color:red}</style><p>after</p>")
    text = extractor.finish()

    assert "before" in text and "after" in text
    assert "color:red" not in text


def test_an_unterminated_comment_releases_its_text_at_eof():
    """Also asserted on the extractor directly, for the same masking reason."""
    extractor = inbound._BodyExtractor()
    extractor.feed("<p>before</p><!--x<p>AFTER</p>")
    text = extractor.finish()

    assert "AFTER" in text, "the rest of the document was swallowed by a comment"
    assert "<!" not in text and "<p>" not in text, f"raw markup leaked: {text!r}"


def test_a_terminated_comment_is_still_dropped_whole():
    extractor = inbound._BodyExtractor()
    extractor.feed("<p>before</p><!-- hidden --><p>after</p>")
    text = extractor.finish()

    assert "before" in text and "after" in text and "hidden" not in text


def test_the_empty_output_fallback_fires_when_the_parser_yields_nothing(monkeypatch):
    """The safety net itself, forced — otherwise nothing ever exercises it.

    It is the last thing standing between a parser bug and a destroyed enquiry,
    so "it never fires in practice" is not a reason to leave it unverified.
    """
    class Blank(inbound._BodyExtractor):
        def finish(self):
            return "   "

    monkeypatch.setattr(inbound, "_BodyExtractor", Blank)
    body = inbound.strip_html("<p>Traveler: Emma M.</p><p>Is it available?</p>")

    assert "Emma M." in body and "Is it available?" in body


def test_the_extractor_never_returns_a_blank_body_for_a_document_with_text():
    """Last-resort net. `accept` rejects an unparseable item and the webhook still
    answers 202, so "no text" is indistinguishable from "no enquiry"."""
    for html in ("<style>x", "<!--", "<![CDATA[", "<script>", "<!--[if mso]>"):
        html_with_text = html + "Emma wrote in about the unit"
        assert inbound.extract_body({"HtmlBody": html_with_text}).strip(), html


@pytest.mark.parametrize("text,shown", [
    # Regression: entity refs were re-emitted as `&name;` from html.parser's
    # `handle_entityref`, which matches `&name` with NO semicolon. That inserted
    # a character the guest never typed, corrupting ordinary mail and moving the
    # message id on rollout. None of these contain a bare `<`.
    ("Is there a Q&A night?", "Q&A night?"),
    ("Is it like a B&B?", "B&B?"),
    ("I work for AT&T.", "AT&T."),
    ("Johnson&Johnson relocation", "Johnson&Johnson relocation"),
    ("See https://ff.com/x?a=1&b=2", "?a=1&b=2"),
    ("Rent is&nbsp $2400", "is&nbsp $2400"),
    # ...while real entities must still pass through undecoded.
    ("Budget &lt; $2000 &amp; flexible", "&lt; $2000 &amp; flexible"),
    ("it&#39;s available", "it&#39;s available"),
])
def test_an_ampersand_is_never_rewritten(text, shown):
    body = inbound.extract_body({"HtmlBody": f"<p>{text}</p>"})
    assert shown in body, f"expected {shown!r} in {body!r}"


def test_an_incomplete_character_reference_does_not_dump_raw_markup():
    """A `&#` in a guest's URL made html.parser abandon its scan and return every
    remaining byte as text, so the whole notification landed in the body as
    markup and `parse` found nothing."""
    html = ("<html><body><p>I saw https://ff.com/l?id=9&#details</p>" + TABLE +
            "<p>Available?</p></body></html>")
    body = inbound.extract_body({"HtmlBody": html})
    item = ff_email.parse(MESSAGE_SUBJECT, body)

    assert "<td>" not in body and "<table>" not in body, f"raw markup leaked: {body!r}"
    assert item is not None and item["sender"] == "Emma M."


@pytest.mark.parametrize("cell,expected", [
    ("Emma&Ryan", "Emma&Ryan"),
    ("T&J M.", "T&J M."),
    ("Emma &amp; Ryan", "Emma &amp; Ryan"),
])
def test_an_ampersand_in_the_guest_name_is_not_rewritten(cell, expected):
    """Lead ids don't hash the body, but they do hash the name — and the name is
    what the drafter addresses the guest by."""
    html = ("<html><body><table>"
            "<tr><td>Property</td><td>Sunny 1BR</td></tr>"
            f"<tr><td>Traveler</td><td>{cell}</td></tr>"
            "<tr><td>Date received</td><td>8/14/26</td></tr>"
            "</table><p>Is it available?</p></body></html>")
    item = ff_email.parse(MESSAGE_SUBJECT, inbound.extract_body({"HtmlBody": html}))
    assert item is not None and item["sender"] == expected


@pytest.mark.parametrize("html,expected", [
    ("<div>line one<br>line two</div>", "line one\nline two"),
    ("<div>line one<br/>line two</div>", "line one\nline two"),
    ("<div>line one<br />line two</div>", "line one\nline two"),
    # `</p>` closes with a newline and the next `<p>` opens with the separator
    # space, exactly as the old substitution ordered them.
    ("<p>para one</p><p>para two</p>", "para one\n para two"),
])
def test_block_boundaries_still_become_newlines(html, expected):
    """Mutation testing found this contract had no coverage at all: breaking
    `<br>` → newline left all 24 tests green."""
    assert expected in inbound.extract_body({"HtmlBody": html})


def _growth_ratio(make):
    """Median-of-3 wall time at n and 2n. Ratio ~2 is linear, ~4 is quadratic."""
    timings = {}
    for n in (4000, 8000):
        payload = make(n)
        runs = []
        for _ in range(3):
            start = time.perf_counter()
            inbound.extract_body({"HtmlBody": payload})
            runs.append(time.perf_counter() - start)
        timings[n] = sorted(runs)[1]
    return timings[8000] / max(timings[4000], 1e-6)


def test_html_fallback_stays_linear_on_bare_less_than_prose():
    """Base's `<[^>]+>` is itself super-linear here — 5.5s on a 512KB payload.

    The threshold is loose on purpose: this is a shape tripwire for a quadratic
    regression, not a benchmark, and CI timing is noisy.
    """
    ratio = _growth_ratio(
        lambda n: "<p>" + ("budget < 2400 and more text here " * (n // 30)) + "</p>")
    assert ratio < 3.0, f"bare-< prose scaling looks quadratic: {ratio:.2f}x per doubling"


def test_unclosed_script_openers_stay_linear():
    """The worst shape on base: `<(script|style).*?</\\1>` backtracks over every
    opener looking for a close that never comes. Base exceeds 30s at the 512KB
    payload cap; a parser is unaffected because it never backtracks."""
    ratio = _growth_ratio(lambda n: "<p>hi</p>" + ("<script>" * n))
    assert ratio < 3.0, f"unclosed-opener scaling looks quadratic: {ratio:.2f}x per doubling"


def test_extraction_never_raises_on_malformed_markup():
    """`accept` turns any exception here into a dropped enquiry with a 202, so
    the extractor must fail open on whatever an ESP emits."""
    for junk in ("<", "<<<>>>", "<p", "<!--", "<![CDATA[", "<?", "</>", "<a href=<b>",
                 "&", "&#;", "\x00<p>x</p>", "<p>" + "<" * 500):
        assert isinstance(inbound.extract_body({"HtmlBody": junk}), str)
