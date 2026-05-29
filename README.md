# str_leads — real-browser lead/message checker

Spins up a real (non-sandboxed) Chrome via Playwright, logs into your sites, and surfaces only **new** leads/messages since the last run.

## Setup

```bash
cd /Users/sagiv/workspace/str_leads
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chrome      # uses real Google Chrome
cp .env.example .env           # then edit .env with credentials
```

## Run (CLI)

```bash
python check_leads.py             # one shot
python check_leads.py --loop 300  # every 5 min
```

A visible Chrome window opens (set `HEADLESS=1` in `.env` to hide). Browser state is persisted in `./browser_profile/` so you stay logged in across runs.

## Dashboard

A small Flask dashboard shows the last 20 leads and 20 messages and lets you trigger a check (with in-browser OTP entry).

```bash
python dashboard.py               # then open http://localhost:5000
```

- The tables load immediately from `leads.db`.
- Click **Check now** to run the real-browser scrape. A Chrome window opens and the status banner shows progress.
- If FurnishedFinder requires an OTP, the banner switches to **waiting for OTP** and an input appears — paste the code and Submit (this replaces writing to `./OTP_CODE`).
- When the run finishes, the tables refresh; new items are also notified (see below). A second run reports nothing new (dedup).

## Auto-responder agent

The dashboard can draft personalized replies to new leads, pick the right unit, and skip poor fits. Drafts are reviewed by you — **nothing is sent without a click**.

Setup:

1. Set `ANTHROPIC_API_KEY` in `.env`.
2. Fill in `units.json` with your real units (price, occupancy, pets, stay length, area, notes). Each unit's `listing_match` is a substring that ties a lead's listing title to that unit (e.g. `"Unit 1"`).
3. Optionally tune the voice in `response_template.md`.

How it works:

- On each **Check now**, every *new* lead is evaluated by Claude (`claude-sonnet-4-6`). It judges fit in priority order — **area → budget → occupancy/pets/lease-length** — picks the single best-fitting unit, and writes a draft. Poor fits are **Skipped** with a reason.
- Each lead's **Reply** column shows the decision: a skip reason, or the chosen unit + an editable draft.
- Edit the draft if you like, then click **Send**. After you confirm, a browser opens and automates FurnishedFinder's "Reply To Tenant"; the row flips to **Sent ✓**. **Re-draft** regenerates; **Dismiss** hides it.
- The agent only states facts present in `units.json` — it won't invent amenities, prices, or availability.

> First live send: the reply-composer selectors are confirmed against the real page; if they miss, the error appears in the status banner and the selectors in `sites/furnishedfinder.py::send_reply` can be adjusted (same approach used to nail down the OTP login).

### Reply channels (platform + email)

`REPLY_CHANNELS` (default `platform,email`) controls what **Send** does:

- **platform** — automates FurnishedFinder's "Reply To Tenant" (the source of truth for `Sent ✓`).
- **email** — also emails the tenant directly via SMTP. The tenant's address is extracted from the inquiry at draft time (it usually appears in message bodies, not lead rows). If no address was found or SMTP isn't configured, the platform reply still goes and the banner notes that email was skipped. A failed email is reported via notification but never blocks the platform reply.

Set `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASS`/`FROM_EMAIL` in `.env` to enable email (Gmail needs an **App Password**). Drop `email` from `REPLY_CHANNELS` to reply on-platform only.

### Scheduling twice a day

For a hands-off twice-daily check (drafts ready when you open the dashboard), run the dashboard as a service and trigger `/refresh` on a timer — the scrape then runs inside the dashboard process, which auto-drafts new leads and handles OTP. See **[DEPLOY.md](DEPLOY.md)** for the Ubuntu VM setup (systemd service + timer at 08:00 & 18:00). When a check needs an OTP you'll get a notification; open the dashboard and paste the code into the still-waiting run.

## Adding a site

1. Copy `sites/example_site.py` to `sites/<your_site>.py`.
2. Edit the `_is_logged_in`, `_login`, and `check` selectors for that site.
3. Add matching `SITE2_*` env vars in `.env` and read them in the new adapter.
4. Import and append to `SITES` in `check_leads.py`.

## How dedup works

`storage.py` records each item by `(site, kind, id)` in `leads.db`. Only items with an unseen `id` are reported. Make sure your adapter's `id` field is stable across page loads — usually a URL slug or DOM `data-*` attribute, not the row index.

## Notifications

- Always: stdout + `check_leads.log`
- macOS: native notification banner
- Webhook (Slack/Discord/etc.): set `NOTIFY_WEBHOOK_URL` in `.env`
