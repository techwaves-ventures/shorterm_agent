"""FurnishedFinder landlord lead/message checker (email + OTP login via modal)."""
import hashlib
import logging
import os
import re

# Optional at import time so the web app (which imports this adapter via runner)
# boots on hosts without Playwright, e.g. Vercel serverless. Actual scraping
# only runs where Playwright + Chromium are installed. `Page` is used solely as
# a type hint; PWTimeout only in except-clauses reached during a live scrape.
try:
    from playwright.sync_api import Page, TimeoutError as PWTimeout
except ImportError:  # pragma: no cover - depends on deploy target
    Page = "Page"

    class PWTimeout(Exception):
        pass


log = logging.getLogger(__name__)

SITE_NAME = "furnishedfinder"
HOME_URL = "https://www.furnishedfinder.com/"
LEADS_URL = "https://www.furnishedfinder.com/members/tenant-lead"
MESSAGES_URL = "https://www.furnishedfinder.com/members/tenant-message"

# Optional UI hook: set by the dashboard runner to surface progress (e.g. the
# moment we start waiting for an OTP). Signature: STATUS_CB(state, message).
# When None (the CLI case) behavior is unchanged.
STATUS_CB = None

# Per-run account context, set by the runner before a scrape/send so this
# single-account module can drive a specific tenant's FF login. Runs are
# serialized by the runner's global lock, so a module-level context is safe.
# When None (the CLI case) the FF_USERNAME env + ./OTP_CODE file flow is used.
#   {"username": str, "otp_provider": callable|None}
_CONTEXT: dict | None = None


class FurnishedFinderBlocked(RuntimeError):
    """Raised when FurnishedFinder serves a bot/security challenge page."""

    fatal_scrape = True
    user_safe_message = (
        "FurnishedFinder blocked the browser check with a security challenge. "
        "The worker/browser access needs to be adjusted before verification can complete."
    )

    def __init__(self, context: str):
        super().__init__(self.user_safe_message)
        self.context = context


class FurnishedFinderLoginLinkRequired(RuntimeError):
    """Raised when FF asks for an emailed magic link but the submitted value is not one."""

    user_safe_message = (
        "FurnishedFinder sent a magic login link, not a short code. "
        "Click Check now again, then paste the full https://www.furnishedfinder.com/... link from the email."
    )

    def __init__(self):
        super().__init__(self.user_safe_message)


def set_context(username: str, otp_provider=None, status_cb=None) -> None:
    """Bind the FF account for the next run. `otp_provider()` (if given) is called
    to obtain an OTP code (blocking) instead of polling the global ./OTP_CODE.
    Resets the per-page login flag so the new account/profile re-authenticates."""
    global _CONTEXT, STATUS_CB, _session_logged_in
    _CONTEXT = {"username": username or "", "otp_provider": otp_provider}
    STATUS_CB = status_cb
    _session_logged_in = False


def clear_context() -> None:
    global _CONTEXT, STATUS_CB, _session_logged_in
    _CONTEXT = None
    STATUS_CB = None
    _session_logged_in = False


def _username() -> str:
    if _CONTEXT is not None:
        return _CONTEXT.get("username", "")
    return os.getenv("FF_USERNAME", "")


def _hash(*parts: str) -> str:
    return hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()[:16]


def _mask_email(email: str) -> str:
    """j***@example.com — keep FF emails out of logs in full."""
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def _prompt_for_otp(prompt_message: str | None = None) -> str:
    import sys, time
    from pathlib import Path

    prompt_message = prompt_message or (
        "FurnishedFinder sent a login code or magic link to your email — enter it below."
    )
    print("\n" + "=" * 60, flush=True)
    print(">> FurnishedFinder sent a login code/link to your email.", flush=True)
    print(">> Write the code/link to ./OTP_CODE (or set OTP_CODE env var).", flush=True)
    print("=" * 60, flush=True)

    env_code = os.getenv("OTP_CODE", "").strip()
    if env_code:
        return env_code

    if STATUS_CB:
        try:
            STATUS_CB("waiting_for_otp", prompt_message)
        except Exception:
            pass

    # Dashboard path: a per-tenant provider blocks until the tenant submits a
    # code on their own dashboard (routes to the correct run; see runner).
    if _CONTEXT is not None and _CONTEXT.get("otp_provider"):
        code = _CONTEXT["otp_provider"]()
        if not code:
            raise RuntimeError("Timed out waiting for OTP.")
        log.info("Got OTP code from dashboard provider")
        return code

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


def _is_furnishedfinder_magic_link(value: str) -> bool:
    value = (value or "").strip().lower()
    return value.startswith("https://") and "furnishedfinder.com/" in value


def _complete_magic_link_login(page: Page, login_link: str) -> None:
    if not _is_furnishedfinder_magic_link(login_link):
        raise FurnishedFinderLoginLinkRequired()
    log.info("Opening FurnishedFinder magic login link")
    page.goto(login_link.strip(), wait_until="domcontentloaded")
    page.wait_for_timeout(5000)
    _raise_if_blocked(page, "magic login link")
    if not _session_ok(page):
        raise RuntimeError("FurnishedFinder magic link did not establish a session.")


def _body_text_sample(page: Page) -> str:
    try:
        return (page.locator("body").inner_text(timeout=1500) or "")[:4000]
    except Exception:
        return ""


def _login_dialog_present(page: Page) -> bool:
    """True when FF rendered the login dialog inside an otherwise OK page.

    FurnishedFinder can keep the destination title/url (for example
    "Tenant Leads") while showing only the login modal in the body. Treating
    that as a live session makes the scraper parse the login shell as zero
    leads, so the session probe must look at the rendered body too.
    """
    body = _body_text_sample(page).lower()
    if (
        "login to your account" in body
        and ("forgot username or password" in body or "not a member yet" in body)
    ):
        return True
    try:
        if page.locator("input#username").first.is_visible(timeout=800):
            return True
    except Exception:
        pass
    try:
        if page.locator('button[type="submit"]:has-text("Login")').first.is_visible(timeout=800):
            return True
    except Exception:
        pass
    return False


def _cloudflare_challenge_reason(page: Page) -> str:
    title = ""
    try:
        title = (page.title() or "").strip()
    except Exception:
        pass
    url = (getattr(page, "url", "") or "").strip()
    body = _body_text_sample(page)
    haystack = "\n".join([title, url, body]).lower()
    if "attention required" in haystack and "cloudflare" in haystack:
        return f"title={title!r} url={url!r}"
    if "cloudflare ray id" in haystack:
        return f"title={title!r} url={url!r}"
    if "checking if the site connection is secure" in haystack:
        return f"title={title!r} url={url!r}"
    return ""


def _raise_if_blocked(page: Page, context: str) -> None:
    reason = _cloudflare_challenge_reason(page)
    if reason:
        log.warning("FurnishedFinder security challenge during %s: %s", context, reason)
        raise FurnishedFinderBlocked(context)


def _open_login_modal(page: Page) -> None:
    page.goto(HOME_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    _raise_if_blocked(page, "login home")
    page.evaluate("document.querySelector('#nav-login')?.click()")
    page.wait_for_selector("input#username", timeout=10000)


def _login(page: Page) -> None:
    if not _username():
        raise RuntimeError("FF_USERNAME not set in .env — cannot auto-login.")

    log.info("Opening login modal for %s", _mask_email(_username()))
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
        login_link = _prompt_for_otp(
            "FurnishedFinder sent a magic login link to your email. Paste the full link here."
        )
        _complete_magic_link_login(page, login_link)
        return

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
    _raise_if_blocked(page, "session probe")
    title = (page.title() or "").strip()
    if not title or "403" in title:
        return False
    try:
        if page.locator("a#nav-login").first.is_visible(timeout=1000):
            return False
    except PWTimeout:
        pass
    if _login_dialog_present(page):
        return False
    return True


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}


def _norm_date(s: str) -> str:
    """Normalize 'Jun 13, 2026' -> '6/13/26' so detail dates match the row style."""
    m = re.match(r"([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", s.strip())
    if not m:
        return s.strip()
    mon = _MONTHS.get(m.group(1).lower())
    if not mon:
        return s.strip()
    return f"{mon}/{int(m.group(2))}/{m.group(3)[-2:]}"


def _kv(detail: str, label: str) -> str:
    """Pull a value from FurnishedFinder's label/value detail layout.

    The detail page renders each field as a label line followed by its value on
    the next non-empty line, e.g.  "Travelers:\n1".
    """
    target = label.lower().rstrip(":")
    lines = [ln.strip() for ln in detail.splitlines()]
    for i, ln in enumerate(lines):
        if ln.lower().rstrip(":") == target:
            for v in lines[i + 1:]:
                if v:
                    return v
            return ""
    return ""


def _parse_lead_detail(detail: str) -> dict:
    """Best-effort extraction of structured facts from a lead's detail page.

    The detail page is a label/value list (Requested travel dates, Travelers,
    Budget, Reason for travel, Traveling with pets, Occupation, Work location).
    All fields are optional — only keys we actually find are returned, so older
    data (or a layout we don't recognize) degrades gracefully.
    """
    facts: dict = {}
    if not detail:
        return facts

    occ = _kv(detail, "Travelers")
    if occ.isdigit():
        facts["occupants"] = int(occ)

    pets = _kv(detail, "Traveling with pets")
    if pets:
        facts["pets"] = pets.strip().lower() not in ("no", "false", "none", "0", "-")

    budget = _kv(detail, "Budget")
    if budget and budget != "-":
        facts["budget"] = budget

    for label, key in (
        ("Reason for travel", "reason"),
        ("Occupation", "occupation"),
        ("Work location", "work_location"),
    ):
        val = _kv(detail, label)
        if val and val != "-":
            facts[key] = val

    # Travel range on the detail page ("Jun 13, 2026 - Jul 13, 2026"); the row
    # already carries M/D/YY dates, so these are a fallback (see _extract_leads).
    rng = re.search(
        r"([A-Za-z]{3}\.?\s+\d{1,2},?\s+\d{4})\s*-\s*([A-Za-z]{3}\.?\s+\d{1,2},?\s+\d{4})",
        detail,
    )
    if rng:
        facts["move_in"] = _norm_date(rng.group(1))
        facts["move_out"] = _norm_date(rng.group(2))

    phone = re.search(
        r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", detail
    )
    if phone:
        facts["phone"] = phone.group(0).strip()

    email = _EMAIL_RE.search(detail)
    if email:
        facts["email"] = email.group(0)

    return facts


# Labels that mark the lead inquiry block on the detail page. The block is found
# by scoring visible containers on these and taking the tightest match.
_INQUIRY_KEYS = [
    "requested travel dates", "travelers", "budget", "reason for travel",
    "traveling with pets", "date received", "occupation", "work location",
    "number of bedrooms", "desired amenities",
]


def _find_inquiry_block(page: Page) -> str:
    """Return the text of the lead-detail inquiry block on the current page.

    Clicking a lead navigates to a full detail route (no modal). The inquiry is
    one structured block among lots of nav chrome; we score visible containers
    by how many known field labels they contain and return the tightest match,
    so we get the inquiry without the surrounding page furniture.
    """
    js = """(keys) => {
      let best = null;
      for (const el of document.querySelectorAll('div,section,article')) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        const t = (el.innerText || '').trim();
        if (!t || t.length > 2500) continue;
        const low = t.toLowerCase();
        let score = 0;
        for (const k of keys) if (low.includes(k)) score++;
        if (score >= 4 && (best === null || t.length < best.len))
          best = {text: t, len: t.length};
      }
      return best ? best.text : '';
    }"""
    try:
        return (page.evaluate(js, _INQUIRY_KEYS) or "").strip()
    except Exception:
        return ""


def _real_lead_rows(page: Page) -> list:
    """Visible lead <tr> rows (header skipped) as (locator, text) pairs."""
    out = []
    for r in page.locator("table tr").all():
        try:
            t = (r.inner_text() or "").strip()
        except Exception:
            continue
        if not t or len(t) < 20:
            continue
        low = t.lower()
        if "property + traveler" in low or "travel dates" in low:
            continue
        out.append((r, t))
    return out


def _scrape_lead_detail(page: Page, row, move_in: str) -> str:
    """Click a lead row open and return its detail-page inquiry text.

    The travel-dates cell navigates to the lead's `?leadId=…` detail route (it
    is NOT a modal). We click, wait for that route, capture the inquiry block,
    and leave the caller to navigate back to the list. Returns "" on failure so
    the caller falls back to the row text.
    """
    clicked = False
    sels = []
    if move_in:
        sels = [
            f'td:has-text("{move_in}")',
            f'a:has-text("{move_in}")',
            f'button:has-text("{move_in}")',
        ]
    for sel in sels:
        try:
            el = row.locator(sel).first
            if el.is_visible(timeout=800):
                el.click()
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        try:
            row.click()
            clicked = True
        except Exception:
            return ""

    try:
        page.wait_for_url("**leadId=**", timeout=5000)
    except Exception:
        pass  # capture whatever rendered even if the URL pattern shifts
    page.wait_for_timeout(1500)
    return _find_inquiry_block(page)


def _extract_leads(page: Page) -> list[dict]:
    """The leads page is a real <table>. Each <tr> is one lead.

    Phase 1 collects all rows (row text + ids). Phase 2 (when DETAIL_SCRAPE is
    on) opens each lead's travel-dates detail view to capture the full inquiry
    (move-in/out, occupants, pets, budget, reason, contact) for review + drafts.
    """
    items: list[dict] = []
    try:
        page.wait_for_selector("table tr", timeout=10000)
    except PWTimeout:
        return items

    # --- Phase 1: collect rows ------------------------------------------------
    collected: list[dict] = []
    for _r, text in _real_lead_rows(page):
        try:
            # A lead row looks like:
            #   <unit> <area> <traveler>  <move-in> - <move-out> (N nights)
            #   $<price>  <date-received>  Reply To Tenant
            # so multiple M/D/YY dates appear. The FIRST is the move-in date,
            # the SECOND is the move-out date (the travel range), and the LAST
            # standalone M/D/YY (after the price) is when the lead was RECEIVED.
            all_dates = re.findall(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", text)
            received = all_dates[-1] if all_dates else ""
            move_in = all_dates[0] if all_dates else ""
            move_out = all_dates[1] if len(all_dates) >= 3 else ""
            nights_m = re.search(r"\((\d+)\s*nights?\)", text, re.I)
            # Traveler line is "<first name> <last initial>", e.g. "Svetlana V."
            # or "FELICIEN P." — the first name may be all-caps, so match a
            # letter run (not strictly Titlecase) followed by a single initial.
            traveler = ""
            for line in text.splitlines():
                line = line.strip()
                if re.match(r"^[A-Z][A-Za-z'’-]+ [A-Z]\.?$", line):
                    traveler = line
                    break

            # Hash on the full row text (which already contains both dates) +
            # traveler, so the id stays stable regardless of how we interpret
            # the individual dates.
            iid = _hash(traveler, text[:120])
            # Pull a short title.
            title_lines = [
                ln.strip()
                for ln in text.splitlines()
                if ln.strip() and ln.strip() not in ("Reply To Tenant",)
            ]
            title = " | ".join(title_lines[:3])[:240]
            item = {
                "id": iid,
                "title": title,
                "url": LEADS_URL,
                "received": received,
                "move_in": move_in,
                "traveler": traveler,
                # Full row text — the responder agent reads this for dates,
                # occupancy, budget, etc. Not part of the id hash, so adding
                # it doesn't change dedup.
                "raw": text,
            }
            if move_out:
                item["move_out"] = move_out
            if nights_m:
                item["nights"] = int(nights_m.group(1))
            collected.append(item)
        except Exception:
            continue

    # --- Phase 2: enrich with the detail page --------------------------------
    # Clicking a lead navigates to its detail route, so the Phase-1 row handles
    # go stale. We re-fetch the rows by index each iteration, match the item by
    # its move_in date, scrape the detail, then navigate back to the list.
    detail_on = os.getenv("DETAIL_SCRAPE", "1").strip().lower() not in ("0", "false", "no")
    if not detail_on:
        return collected

    for item in collected:
        move_in = item.get("move_in", "")
        try:
            # Locate this lead's current row (handles are stale after navigation).
            row = None
            for r, text in _real_lead_rows(page):
                if _hash(item.get("traveler", ""), text[:120]) == item["id"]:
                    row = r
                    break
            if row is None:
                continue

            detail = _scrape_lead_detail(page, row, move_in)
            if detail:
                item["detail"] = detail
                facts = _parse_lead_detail(detail)
                # Prefer row-derived dates; fill only what the row lacked.
                for k, v in facts.items():
                    if k in ("move_in", "move_out") and item.get(k):
                        continue
                    item[k] = v
            else:
                log.info("Lead detail empty for %r (selector miss)", item.get("traveler"))
        except Exception:
            log.exception("Lead detail scrape failed for %r", item.get("traveler"))
        finally:
            # Always return to the leads list for the next iteration.
            try:
                if "leadId=" in page.url:
                    page.goto(LEADS_URL, wait_until="domcontentloaded")
                    page.wait_for_timeout(1500)
                    page.wait_for_selector("table tr", timeout=10000)
            except Exception:
                log.exception("Failed returning to leads list")

    return collected


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
    _raise_if_blocked(page, "leads page")
    for it in _extract_leads(page):
        it["kind"] = "lead"
        out.append(it)

    page.goto(MESSAGES_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    log.info("Messages page: %s (title=%r)", page.url, page.title())
    _raise_if_blocked(page, "messages page")
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


def _find_lead_row(page: Page, lead: dict, traveler: str, received: str):
    """Locate the table row for a lead, tolerant of a missing traveler name.

    The traveler is only captured when the row matches a "Firstname X." shape,
    so some leads have traveler=''. Rather than fail, score every row on the
    signals we *do* have — traveler, the received date, the travel range
    (move_in/move_out), and distinctive tokens from the stored `raw` row text —
    and take the best non-trivial match. This makes sending robust even when the
    name wasn't parsed.
    """
    move_in = (lead.get("move_in") or "").strip()
    move_out = (lead.get("move_out") or "").strip()
    # Strip the boilerplate action label so it doesn't become a token shared by
    # every row (which would defeat the score threshold below).
    raw = (lead.get("raw") or "").replace("Reply To Tenant", "")
    # Distinctive tokens from the original row: prices, dollar amounts, the unit
    # name — anything that helps disambiguate when there's no name.
    raw_tokens = set(re.findall(r"\$[\d,]+|\b[A-Z][a-zA-Z]{3,}\b|\d{1,2}/\d{1,2}/\d{2,4}", raw))

    best = None
    best_score = 0
    for r in page.locator("table tr").all():
        try:
            rtext = r.inner_text() or ""
        except Exception:
            continue
        if not rtext.strip() or "Reply To Tenant" not in rtext:
            continue
        score = 0
        if traveler and traveler in rtext:
            score += 5
        if received and received in rtext:
            score += 3
        if move_in and move_in in rtext:
            score += 2
        if move_out and move_out in rtext:
            score += 2
        # Token overlap as a tie-breaker / fallback when names are absent.
        if raw_tokens:
            overlap = sum(1 for t in raw_tokens if t in rtext)
            score += min(overlap, 4)
        if score > best_score:
            best_score, best = score, r

    # Require a meaningful match (more than a single incidental token) so we
    # never reply on the wrong lead.
    return best if best_score >= 2 else None


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

    target = _find_lead_row(page, lead, traveler, received)
    if target is None:
        raise RuntimeError(
            f"Could not find lead row for {traveler!r} ({received!r})."
        )

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


def _fill_and_submit_composer(page: Page, text: str, who: str) -> None:
    """Shared composer fill+submit, used by the message-thread reply flow."""
    compose_sel = (
        "textarea, "
        '[contenteditable="true"], '
        'input[name*="message" i], textarea[name*="message" i], '
        'textarea[placeholder*="message" i], '
        'textarea[placeholder*="reply" i], textarea[placeholder*="type" i]'
    )
    try:
        page.wait_for_selector(compose_sel, timeout=10000)
    except PWTimeout:
        raise RuntimeError("Message composer textarea did not appear.")
    box = page.locator(compose_sel).last  # the open thread's box is last in DOM
    box.click()
    box.fill(text)
    page.wait_for_timeout(500)

    for sel in (
        'button:has-text("Send")',
        'button:has-text("Send Message")',
        'button:has-text("Reply")',
        'button:has-text("Submit")',
        'button[type="submit"]',
    ):
        try:
            btn = page.locator(sel).last
            if btn.is_visible(timeout=1500):
                btn.click()
                page.wait_for_timeout(2500)
                log.info("Message reply sent to %s", who)
                return
        except PWTimeout:
            continue
    raise RuntimeError("Could not find a Send button to submit the message reply.")


def send_message_reply(page: Page, item: dict, text: str) -> None:
    """Reply inside an existing FurnishedFinder message thread.

    The messages page is a SPA conversation list (no per-row anchors), so we
    open the conversation by matching the sender name (and date when available)
    in the list, then fill + submit the composer. Defensive multi-selector
    candidates throughout — exact selectors are confirmed on the first live send,
    same as the lead reply and OTP flows.
    """
    _ensure_session(page)
    page.goto(MESSAGES_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    sender = (item.get("sender") or item.get("traveler") or "").strip()
    if not sender:
        raise RuntimeError("Message has no sender name to locate the conversation.")

    # Open the conversation. Try a text match on the sender name across the
    # common clickable containers the SPA uses for conversation rows.
    opened = False
    for sel in (
        f'[role="listitem"]:has-text("{sender}")',
        f'li:has-text("{sender}")',
        f'div[class*="conversation" i]:has-text("{sender}")',
        f'div[class*="thread" i]:has-text("{sender}")',
        f'a:has-text("{sender}")',
        f'*:has-text("{sender}")',
    ):
        try:
            row = page.locator(sel).first
            if row.is_visible(timeout=1500):
                row.click()
                page.wait_for_timeout(1500)
                opened = True
                break
        except PWTimeout:
            continue
    if not opened:
        raise RuntimeError(f"Could not find a message conversation for {sender!r}.")

    _fill_and_submit_composer(page, text, sender)
