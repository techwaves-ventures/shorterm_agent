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
from playwright.sync_api import sync_playwright

load_dotenv()

from notify import notify
from storage import filter_new
from sites import furnishedfinder

LOG_PATH = Path(__file__).parent / "check_leads.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("check_leads")

PROFILE_DIR = Path(__file__).parent / "browser_profile"
PROFILE_DIR.mkdir(exist_ok=True)

# Register site adapters here.
SITES = [furnishedfinder]


@contextmanager
def browser_page():
    """Launch a real (non-sandboxed) Chrome with the persistent profile and
    yield a page. Shared by run_scrape and the dashboard's reply-send path."""
    headless = os.getenv("HEADLESS", "0") == "1"
    launch_args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--start-maximized",
    ]
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
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


def run_scrape(status_cb=None, on_new_items=None) -> dict:
    """Launch a real browser, scrape every registered site, dedup + notify.

    `status_cb(state, message)` is invoked at key transitions so a UI (the Flask
    dashboard) can show progress. It is optional — the CLI passes nothing.
    `on_new_items(site, kind, new_items)` is called for each batch of newly-seen
    items (used by the dashboard to auto-draft replies).

    Returns a dict of new-item counts: {site: {kind: n}}.
    """

    def emit(state: str, message: str = "") -> None:
        if status_cb:
            try:
                status_cb(state, message)
            except Exception:
                log.exception("status_cb failed")

    counts: dict[str, dict] = {}
    emit("launching", "Launching browser…")

    with browser_page() as page:
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
                new_items = filter_new(name, kind, kind_items)
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
                        on_new_items(name, kind, new_items)
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
