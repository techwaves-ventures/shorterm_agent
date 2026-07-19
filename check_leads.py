"""Spin up a real (non-sandboxed) Chrome via Playwright and scan sites for new leads.

Usage:
    python check_leads.py                 # run once
    python check_leads.py --loop 300      # run every 300 seconds

The browser uses a persistent profile under ./browser_profile so cookies/session
survive across runs. The first run for a site may require manual login; after
that, the env-supplied credentials only kick in if the session expires.
"""
import argparse
import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv

# Playwright is optional at import time: the web app imports this module (via
# runner) just to expose the scrape entrypoints, but a serverless host like
# Vercel neither installs the browser nor can run it. Importing must not fail
# there; run_scrape/browser_page raise a clear error if invoked without it.
try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on deploy target
    sync_playwright = None

load_dotenv()

from notify import notify
from storage import filter_new
from sites import furnishedfinder

# File logging is best-effort: a read-only serverless filesystem (Vercel) can't
# create the log file, so fall back to stdout-only rather than crashing import.
_handlers = [logging.StreamHandler(sys.stdout)]
LOG_PATH = Path(__file__).parent / "check_leads.log"
try:
    _handlers.insert(0, logging.FileHandler(LOG_PATH))
except OSError:  # pragma: no cover - read-only FS
    pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("check_leads")

PROFILE_DIR = Path(__file__).parent / "browser_profile"


def playwright_available() -> bool:
    """True when Playwright (and thus live scraping) can run in this process.

    False on serverless hosts like Vercel, where the app routes scrapes to a
    worker via the shared DB instead of running them in-process.
    """
    return sync_playwright is not None


def _require_playwright():
    """Raise a clear error when a scrape is attempted without Playwright."""
    if sync_playwright is None:
        raise RuntimeError(
            "Playwright is not available in this environment — live scraping is "
            "disabled (e.g. on Vercel serverless). Run scraping from a worker/host "
            "that has Playwright + Chromium installed. See DEPLOY.md."
        )

# Register site adapters here.
SITES = [furnishedfinder]


def _profile_dir(tenant_id: str = "1") -> Path:
    """Per-tenant persistent browser profile (holds the logged-in FF session).

    The operator (tenant '1') keeps the original ./browser_profile so their
    existing session survives; other tenants get ./browser_profiles/{id}/. Each
    profile is an isolated cookie/session store — no cross-tenant session bleed.
    """
    if str(tenant_id) == "1":
        PROFILE_DIR.mkdir(exist_ok=True)
        return PROFILE_DIR
    d = Path(__file__).parent / "browser_profiles" / str(tenant_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def browser_page(tenant_id: str = "1"):
    """Launch a real (non-sandboxed) Chrome with the tenant's persistent profile
    and yield a page. Shared by run_scrape and the dashboard's reply-send path."""
    _require_playwright()
    headless = os.getenv("HEADLESS", "0") == "1"
    launch_args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--start-maximized",
    ]
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(_profile_dir(tenant_id)),
            headless=headless,
            args=launch_args,
            viewport=None,  # use real window size
            ignore_default_args=["--enable-automation"],
            channel="chrome",  # use installed Google Chrome if available
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            yield page
        finally:
            ctx.close()


def run_scrape(status_cb=None, on_new_items=None, tenant_id: str = "1") -> dict:
    """Launch a real browser, scrape every registered site, dedup + notify.

    `status_cb(state, message)` is invoked at key transitions so a UI (the Flask
    dashboard) can show progress. It is optional — the CLI passes nothing.
    `on_new_items(tenant_id, site, kind, new_items)` is called for each batch of
    newly-seen items (used by the dashboard to auto-draft replies).
    `tenant_id` scopes dedup + storage; the CLI defaults to the operator ('1').

    Returns a dict of new-item counts: {site: {kind: n}}.
    """
    _require_playwright()

    def emit(state: str, message: str = "") -> None:
        if status_cb:
            try:
                status_cb(state, message)
            except Exception:
                log.exception("status_cb failed")

    counts: dict[str, dict] = {}
    emit("launching", "Launching browser…")

    with browser_page(tenant_id) as page:
        for site in SITES:
            name = getattr(site, "SITE_NAME", site.__name__)
            log.info("Checking site: %s", name)
            emit("checking", f"Checking {name}…")
            try:
                items = site.check(page)
            except Exception as e:
                log.exception("Site %s failed: %s", name, e)
                continue

            log.info("%s: %d items visible", name, len(items))

            # Group by kind (default "lead") so dedup + notifications stay clean.
            by_kind: dict[str, list[dict]] = {}
            for it in items:
                by_kind.setdefault(it.get("kind", "lead"), []).append(it)

            any_new = False
            for kind, kind_items in by_kind.items():
                new_items = filter_new(tenant_id, name, kind, kind_items)
                if not new_items:
                    continue
                any_new = True
                counts.setdefault(name, {})[kind] = len(new_items)
                body = "\n".join(
                    f"- {it.get('title') or it.get('id')}  ({it.get('url','')})"
                    for it in new_items[:20]
                )
                notify(f"[{name}] {len(new_items)} new {kind}(s)", body)
                if on_new_items:
                    try:
                        on_new_items(tenant_id, name, kind, new_items)
                    except Exception:
                        log.exception("on_new_items hook failed")
            if not any_new:
                log.info("%s: nothing new", name)

    emit("done", "Done.")
    return counts


def run_once() -> None:
    run_scrape()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--loop",
        type=int,
        default=0,
        help="Run repeatedly every N seconds (0 = run once).",
    )
    args = ap.parse_args()

    if args.loop <= 0:
        run_once()
        return

    while True:
        try:
            run_once()
        except Exception:
            log.exception("run_once crashed; will retry")
        log.info("Sleeping %d seconds...", args.loop)
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
