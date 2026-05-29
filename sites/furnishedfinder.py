"""FurnishedFinder landlord lead/message checker (email + OTP login via modal)."""
import hashlib
import logging
import os
import re
from playwright.sync_api import Page, TimeoutError as PWTimeout

log = logging.getLogger(__name__)

SITE_NAME = "furnishedfinder"
HOME_URL = "https://www.furnishedfinder.com/"
LEADS_URL = "https://www.furnishedfinder.com/members/tenant-lead"
MESSAGES_URL = "https://www.furnishedfinder.com/members/tenant-message"

# Optional UI hook: set by the dashboard runner to surface progress (e.g. the
# moment we start waiting for an OTP). Signature: STATUS_CB(state, message).
# When None (the CLI case) behavior is unchanged.
STATUS_CB = None


def _username() -> str:
    return os.getenv("FF_USERNAME", "")


def _hash(*parts: str) -> str:
    return hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()[:16]


def _prompt_for_otp() -> str:
    import sys, time
    from pathlib import Path

    print("\n" + "=" * 60, flush=True)
    print(">> FurnishedFinder sent an OTP to your email.", flush=True)
    print(">> Write the code to ./OTP_CODE (or set OTP_CODE env var).", flush=True)
    print("=" * 60, flush=True)

    env_code = os.getenv("OTP_CODE", "").strip()
    if env_code:
        return env_code

    if STATUS_CB:
        try:
            STATUS_CB("waiting_for_otp", "FurnishedFinder sent an OTP to your email — enter it below.")
        except Exception:
            pass

    # Ping the operator (webhook/desktop) so a scheduled headless run is
    # actionable: they open the dashboard over the network and paste the code
    # into the still-waiting run.
    try:
        from notify import notify
        notify(
            "FurnishedFinder OTP needed",
            "A scheduled check is waiting for an OTP. Open the dashboard and paste the code.",
        )
    except Exception:
        log.exception("OTP notify failed")

    otp_paths = [
        Path(__file__).resolve().parent.parent / "OTP_CODE",
        Path.cwd() / "OTP_CODE",
    ]
    deadline = time.time() + 600
    while time.time() < deadline:
        for p in otp_paths:
            if p.exists():
                code = p.read_text().strip()
                if code:
                    try:
                        p.unlink()
                    except Exception:
                        pass
                    log.info("Got OTP code from %s", p)
                    return code
        if sys.stdin and sys.stdin.isatty():
            try:
                code = input("OTP: ").strip()
                if code:
                    return code
            except EOFError:
                pass
        time.sleep(1)
    raise RuntimeError("Timed out waiting for OTP.")


def _open_login_modal(page: Page) -> None:
    page.goto(HOME_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    page.evaluate("document.querySelector('#nav-login')?.click()")
    page.wait_for_selector("input#username", timeout=10000)


def _login(page: Page) -> None:
    if not _username():
        raise RuntimeError("FF_USERNAME not set in .env — cannot auto-login.")

    log.info("Opening login modal for %s", _username())
    _open_login_modal(page)
    page.fill("input#username", _username())
    page.locator('button[type="submit"]:has-text("Login")').first.click()

    otp_sel = (
        'input[autocomplete="one-time-code"], '
        'input[name="code"], input[name="Code"], '
        'input[name="otp"], input[name="OTP"], '
        'input[inputmode="numeric"], '
        'input[placeholder*="code" i], input[placeholder*="OTP" i]'
    )
    pwd_sel = 'input[type="password"]'
    try:
        page.wait_for_selector(f"{otp_sel}, {pwd_sel}", timeout=25000)
    except PWTimeout:
        raise RuntimeError("After username submit, neither OTP nor password field appeared.")

    if page.locator(pwd_sel).first.is_visible(timeout=1500):
        raise RuntimeError("Site asked for a password, not OTP.")

    code = _prompt_for_otp()
    page.fill(otp_sel, code)

    for sel in (
        'button:has-text("Verify")',
        'button:has-text("Submit")',
        'button:has-text("Continue")',
        'button:has-text("Login")',
        'button[type="submit"]',
    ):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1000):
                btn.click()
                break
        except PWTimeout:
            continue

    page.wait_for_timeout(2500)
    log.info("Login submitted; current URL: %s", page.url)


_session_logged_in = False


def _session_ok(page: Page) -> bool:
    page.goto(LEADS_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    log.info("Session probe: %s (title=%r)", page.url, page.title())
    title = (page.title() or "").strip()
    if not title or "403" in title:
        return False
    try:
        if page.locator("a#nav-login").first.is_visible(timeout=1000):
            return False
    except PWTimeout:
        pass
    return True


def _extract_leads(page: Page) -> list[dict]:
    """The leads page is a real <table>. Each <tr> is one lead."""
    items: list[dict] = []
    try:
        page.wait_for_selector("table tr", timeout=10000)
    except PWTimeout:
        return items

    rows = page.locator("table tr").all()
    for r in rows:
        try:
            text = (r.inner_text() or "").strip()
            if not text or len(text) < 20:
                continue
            # Skip the header row (contains "Property + traveler", "Travel dates", etc.)
            lower = text.lower()
            if "property + traveler" in lower or "travel dates" in lower:
                continue

            # Pull date received (mm/dd/yy or mm/dd/yyyy) and traveler name for a
            # stable, human-readable id.
            date_match = re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", text)
            received = date_match.group(0) if date_match else ""
            # Traveler line typically looks like "Svetlana V." (first name + last initial).
            traveler = ""
            for line in text.splitlines():
                line = line.strip()
                if re.match(r"^[A-Z][a-z]+ [A-Z]\.?$", line):
                    traveler = line
                    break

            iid = _hash(traveler, received, text[:120])
            # Pull a short title.
            title_lines = [
                ln.strip()
                for ln in text.splitlines()
                if ln.strip() and ln.strip() not in ("Reply To Tenant",)
            ]
            title = " | ".join(title_lines[:3])[:240]
            items.append(
                {
                    "id": iid,
                    "title": title,
                    "url": LEADS_URL,
                    "received": received,
                    "traveler": traveler,
                    # Full row text — the responder agent reads this for dates,
                    # occupancy, budget, etc. Not part of the id hash, so adding
                    # it doesn't change dedup.
                    "raw": text,
                }
            )
        except Exception:
            continue
    return items


def _extract_messages(page: Page) -> list[dict]:
    """The messages page is a SPA without per-row anchors. We parse the main
    panel's visible text by splitting on the repeating
        <initials>
        <Full Name>
        <Date>
        <body...>
    pattern that the conversation list uses."""
    items: list[dict] = []
    try:
        page.wait_for_selector("h1:has-text('Messages')", timeout=10000)
        page.wait_for_timeout(2500)
    except PWTimeout:
        return items

    text = page.evaluate(
        """() => {
            const m = document.querySelector('main, [role=main]') || document.body;
            return m.innerText;
        }"""
    ) or ""

    # Trim everything before the first conversation tab (All/Unread/Archived).
    m = re.search(r"\bAll\s+Unread\s+Archived\s*\n", text)
    if m:
        text = text[m.end():]

    # Split on the "<2-3 cap letters>\n<First Last/Initial>\n<Month. Day>" header.
    # Months use abbreviations like "May.", "Apr.", "Jan.", etc.
    pattern = re.compile(
        r"(?m)^([A-Z]{1,3})\n"                              # initials
        r"([A-Z][A-Za-z' .-]{1,60}?)\n"                     # name
        r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2}(?:,?\s*\d{4})?)\n"  # date
    )
    matches = list(pattern.finditer(text))
    for i, m in enumerate(matches):
        initials, name, date = m.group(1), m.group(2).strip(), m.group(3).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        # Trim long body for the notification preview.
        preview = re.sub(r"\s+", " ", body)[:200]

        iid = _hash(name, date, body[:200])
        items.append(
            {
                "id": iid,
                "title": f"{name} ({date}): {preview}",
                "url": MESSAGES_URL,
                "sender": name,
                "date": date,
                # Full message text — the strongest personalization signal for
                # the responder agent. Not part of the id hash.
                "body": body,
            }
        )
    return items


def check(page: Page) -> list[dict]:
    out: list[dict] = []

    _ensure_session(page)

    page.goto(LEADS_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    log.info("Leads page: %s (title=%r)", page.url, page.title())
    for it in _extract_leads(page):
        it["kind"] = "lead"
        out.append(it)

    page.goto(MESSAGES_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    log.info("Messages page: %s (title=%r)", page.url, page.title())
    for it in _extract_messages(page):
        it["kind"] = "message"
        out.append(it)

    return out


def _ensure_session(page: Page) -> None:
    global _session_logged_in
    if not _session_logged_in:
        if not _session_ok(page):
            log.info("No active session — starting OTP login.")
            _login(page)
        _session_logged_in = True


def send_reply(page: Page, lead: dict, text: str) -> None:
    """Open the lead's "Reply To Tenant" composer and send `text`.

    Locates the lead row by traveler + received date, clicks the reply action,
    fills the composer, and submits. Uses defensive multi-selector candidates
    (same approach as the OTP login flow); exact compose selectors are confirmed
    on the first live send.
    """
    _ensure_session(page)
    page.goto(LEADS_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    traveler = (lead.get("traveler") or lead.get("sender") or "").strip()
    received = (lead.get("received") or lead.get("date") or "").strip()

    # Find the row whose text matches this lead, then click its reply action.
    rows = page.locator("table tr").all()
    target = None
    for r in rows:
        try:
            rtext = (r.inner_text() or "")
        except Exception:
            continue
        if traveler and traveler in rtext and (not received or received in rtext):
            target = r
            break
    if target is None:
        raise RuntimeError(f"Could not find lead row for {traveler!r} ({received!r}).")

    clicked = False
    for sel in (
        'button:has-text("Reply To Tenant")',
        'a:has-text("Reply To Tenant")',
        'button:has-text("Reply")',
        'a:has-text("Reply")',
    ):
        try:
            btn = target.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click()
                clicked = True
                break
        except PWTimeout:
            continue
    if not clicked:
        # Fall back to a page-level reply button after selecting the row.
        try:
            target.click()
            page.wait_for_timeout(1000)
            page.locator('button:has-text("Reply"), a:has-text("Reply")').first.click()
            clicked = True
        except Exception as e:
            raise RuntimeError(f"Could not open reply composer: {e}")

    # Fill the composer textarea.
    compose_sel = (
        "textarea, "
        '[contenteditable="true"], '
        'input[name*="message" i], textarea[name*="message" i], '
        'textarea[placeholder*="message" i]'
    )
    try:
        page.wait_for_selector(compose_sel, timeout=10000)
    except PWTimeout:
        raise RuntimeError("Reply composer textarea did not appear.")
    box = page.locator(compose_sel).first
    box.click()
    box.fill(text)
    page.wait_for_timeout(500)

    # Submit.
    for sel in (
        'button:has-text("Send")',
        'button:has-text("Send Message")',
        'button:has-text("Submit")',
        'button[type="submit"]',
    ):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click()
                page.wait_for_timeout(2500)
                log.info("Reply sent to %s", traveler)
                return
        except PWTimeout:
            continue
    raise RuntimeError("Could not find a Send button to submit the reply.")
