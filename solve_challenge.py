"""Open the persistent-profile browser and wait, so a human can clear a
Cloudflare challenge by hand.

When FurnishedFinder's Cloudflare serves an "Attention Required!" page, the
scraper can't get past it on its own — and by design it bails rather than trying.
This opens the SAME persistent profile the scraper uses, navigates to the members
area, and then just waits. You solve the challenge in the window; the fresh
cf_clearance cookie is written into ./browser_profile, and normal "Check now"
runs ride it until it expires again.

    .venv/bin/python solve_challenge.py                 # operator profile
    .venv/bin/python solve_challenge.py --tenant 3      # a specific tenant

Leave the window open until the real FurnishedFinder members page renders (your
leads), then press Enter here to close cleanly.
"""
import argparse
import sys

import check_leads
from sites import furnishedfinder as ff


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="1", help="Tenant whose profile to open (default: operator '1').")
    ap.add_argument("--url", default=ff.LEADS_URL, help="Page to land on.")
    args = ap.parse_args()

    if not check_leads.playwright_available():
        print("Playwright isn't installed here — run this on the browser host.", file=sys.stderr)
        sys.exit(1)

    print("Opening the browser with the persistent profile…")
    print("→ Solve the Cloudflare challenge if it appears, wait for your leads")
    print("  to load, then come back here and press Enter to close.\n")

    # Force a visible window regardless of HEADLESS — you can't solve a challenge
    # you can't see.
    import os
    os.environ["HEADLESS"] = "0"

    with check_leads.browser_page(args.tenant) as page:
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"(navigation note: {e}) — the window is open anyway.")
        try:
            input("Press Enter once your leads are visible to save the session and close… ")
        except (EOFError, KeyboardInterrupt):
            pass
        try:
            print("Current page title:", page.title())
        except Exception:
            pass

    print("Closed. The refreshed Cloudflare clearance is saved in the profile.")


if __name__ == "__main__":
    main()
