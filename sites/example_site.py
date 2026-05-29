"""Template site adapter. Copy this file per site and edit the marked sections.

A site adapter exposes a single function `check(page) -> list[dict]` that:
  1. Logs in if needed (the persistent profile usually keeps you logged in).
  2. Navigates to the leads/messages page.
  3. Extracts items as dicts with at least an "id" key (used for dedup).
"""
import os
from playwright.sync_api import Page

SITE_NAME = os.getenv("SITE1_NAME", "example")
LOGIN_URL = os.getenv("SITE1_LOGIN_URL", "")
LEADS_URL = os.getenv("SITE1_LEADS_URL", "")
USERNAME = os.getenv("SITE1_USERNAME", "")
PASSWORD = os.getenv("SITE1_PASSWORD", "")


def _is_logged_in(page: Page) -> bool:
    # EDIT: return True when a logged-in-only element is visible.
    # Example: presence of a user menu, dashboard link, etc.
    return page.locator("text=Logout").first.is_visible(timeout=2000) if page.url else False


def _login(page: Page) -> None:
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    # EDIT: adjust selectors to your site's login form.
    page.fill('input[name="email"], input[type="email"]', USERNAME)
    page.fill('input[name="password"], input[type="password"]', PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")


def check(page: Page) -> list[dict]:
    """Return list of items currently visible on the leads page."""
    page.goto(LEADS_URL, wait_until="domcontentloaded")

    try:
        if not _is_logged_in(page):
            _login(page)
            page.goto(LEADS_URL, wait_until="domcontentloaded")
    except Exception:
        _login(page)
        page.goto(LEADS_URL, wait_until="domcontentloaded")

    page.wait_for_load_state("networkidle")

    # EDIT: replace with the real selector + extraction for your site.
    # Each item must have a stable "id" so dedup works across runs.
    items: list[dict] = []
    rows = page.locator('[data-lead-id], .lead-row, tr.lead').all()
    for r in rows:
        try:
            iid = r.get_attribute("data-lead-id") or r.inner_text()[:80]
            items.append(
                {
                    "id": iid.strip(),
                    "title": r.locator(".lead-title, td:first-child").first.inner_text().strip()[:200] if r.locator(".lead-title, td:first-child").count() else "",
                    "url": page.url,
                }
            )
        except Exception:
            continue
    return items
