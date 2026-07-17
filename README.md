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

The dashboard can draft personalized replies for both leads and messages, pick the right unit, and skip poor fits. Drafts are reviewed by you — **nothing is sent without a click**.

**Leads vs. Messages — they're different, and the agent treats them differently:**

- A **lead** (📥) is someone who showed interest in your listing but **hasn't written to you**. The draft is a friendly **introduction** that opens the conversation ("Hi Krish, I'd love to welcome you to my place in NW DC…") — it never thanks them for a message they didn't send.
- A **message** (💬) is a tenant who **did write** to you. The draft **responds** to what they actually said, referencing their dates, group size, and questions.

Setup:

1. Set `ANTHROPIC_API_KEY` in `.env`.
2. Fill in `units.json` with your real units (price, occupancy, pets, stay length, area, notes). Each unit's `listing_match` is a substring that ties a lead's listing title to that unit (e.g. `"Unit 1"`).
3. Optionally tune the voice in `response_template.md` — it has separate **Introductions (leads)** and **Replies (messages)** guidance.

How it works:

- On each **Check now**, every *new* lead **and** message is evaluated by Claude (`claude-sonnet-4-6`). It judges fit in priority order — **area → budget → occupancy/pets/lease-length** — picks the single best-fitting unit, and writes a draft (intro for leads, reply for messages). Poor fits (and non-tenant messages, e.g. another host replying) are **Skipped** with a reason.
- For **leads**, the scrape opens each lead's **travel-dates detail view** (the FurnishedFinder panel behind a lead's date range) and captures the full inquiry — move-in/out, length of stay, occupants, pets, budget, reason for the trip, and contact info. This grounds both the review and the draft in the real inquiry rather than the thin table row. Parsed facts also show as quick-fact chips on the card. (Set `DETAIL_SCRAPE=0` to skip the per-lead clicks and fall back to row-only scraping.)
- The dashboard shows two sections (**Leads** / **Messages**) as expandable cards. Each card has a kind badge, a status chip, a **Show full inquiry** toggle that reveals the complete message/lead detail, and — when a draft exists — the chosen unit, the AI's reason, and an editable draft.
- Edit the draft if you like, then click **Send**. After you confirm, a browser opens and automates the platform reply; the card flips to **Sent ✓**. **Re-draft** regenerates; **Dismiss** hides it.
- Filter chips (All / Needs action / Drafted / Sent) and a search box help you work through the list.
- The agent only states facts present in `units.json` — it won't invent amenities, prices, or availability.

> First live send: the platform reply-composer selectors are confirmed against the real page; if they miss, the error appears in the status banner and the selectors in `sites/furnishedfinder.py` (`send_reply` for leads, `send_message_reply` for messages) can be adjusted (same approach used to nail down the OTP login).
>
> First live scrape: the lead travel-dates trigger and detail-panel selectors (`_scrape_lead_detail` in `sites/furnishedfinder.py`) are likewise confirmed against the real page. If they miss, the run still completes with row-only lead data, logs which lead/selector failed, and the candidate selectors can be adjusted in the same defensive multi-selector loop.

### Reply channels (platform + email)

`REPLY_CHANNELS` (default `platform,email`) controls what **Send** does — for **both** leads and messages:

- **platform** — automates the on-platform reply (the source of truth for `Sent ✓`). Leads use the lead-row "Reply To Tenant" button; messages reply inside the message thread.
- **email** — also emails the tenant directly via SMTP. The tenant's address is extracted from the inquiry at draft time (it usually appears in message bodies, not lead rows). If no address was found or SMTP isn't configured, the platform reply still goes and the card notes email was skipped. A failed email is reported via notification but never blocks the platform reply.

Set `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASS`/`FROM_EMAIL` in `.env` to enable email (Gmail needs an **App Password**). Drop `email` from `REPLY_CHANNELS` to reply on-platform only.

### Scheduling twice a day

For a hands-off twice-daily check (drafts ready when you open the dashboard), run the dashboard as a service and trigger `/refresh` on a timer — the scrape then runs inside the dashboard process, which auto-drafts new leads and handles OTP. See **[DEPLOY.md](DEPLOY.md)** for the Ubuntu VM setup (systemd service + timer at 08:00 & 18:00). When a check needs an OTP you'll get a notification; open the dashboard and paste the code into the still-waiting run.

## Demo SaaS (multi-tenant web app)

The dashboard now ships as a small multi-tenant SaaS so it can be shown as a
product, not just a local script. Surfaces:

- **Public landing page** (`/`) — marketing page with pricing and a pilot-access
  waitlist (`/pilot`). Authenticated users are redirected to their dashboard.
- **Auth** (`/login`, `/signup`) — email/password login; each host sees only
  their own tenant's data. The **operator** (tenant 1) owns the pre-existing data.
- **Onboarding** (`/onboarding`) — a guided first-run wizard: host profile → first
  unit → reply tone → optional FurnishedFinder connection → first lead check. New
  tenants are routed here until they finish (or skip).
- **Settings** (`/settings`) — editable units, reply template/tone, sign-off,
  reply channels, notification webhook, FurnishedFinder connect/disconnect, and
  plan status. Sending stays **human-in-the-loop** — the agent drafts, you approve.
- **Billing** (`/billing`) — Stripe subscription plans (Starter/Pro/Portfolio),
  Checkout, and the customer portal. Runs in a safe **demo mode** when no Stripe
  keys are set: "subscribing" marks the tenant as *Pilot (demo)* with no charge.
- **Health check** (`/healthz`) — JSON liveness/readiness probe (reports whether
  the configured DB — SQLite or hosted Postgres — is reachable).

### Database backend

Storage auto-selects at runtime (`db.py`): set `DATABASE_URL` for **hosted
Postgres** (Neon / Vercel Postgres / Supabase), or leave it blank to use
**SQLite** (`SQLITE_PATH`, default `./leads.db`) for local dev. The app speaks a
single SQL dialect and translates for Postgres transparently — same schema, same
per-tenant isolation on both. Postgres is required for serverless (Vercel).

### Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
# Minimum for the web app to boot:
#   SECRET_KEY   -> python -c "import secrets; print(secrets.token_hex(32))"
#   FF_CRED_KEY  -> python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
.venv/bin/python manage.py bootstrap      # create the operator from OPERATOR_EMAIL/PASSWORD
.venv/bin/python manage.py seed-demo      # optional: demo tenant + sample leads/drafts (no real PII)
.venv/bin/python dashboard.py             # http://127.0.0.1:5050
```

`manage.py seed-demo` prints a demo login (`demo@shorterm.test` by default) with
two sample units and a populated inbox of drafted replies waiting for approval —
so you can walk landing → login → dashboard → settings → billing without any real
tenant data. All keys/secrets come from env; missing Stripe/FF secrets fail
gracefully (demo mode / skipped connection). See **[DEPLOY.md](DEPLOY.md)** for
hosted deployment: **Vercel + hosted Postgres** (fastest demo path), Render/Fly
with a persistent disk, or a self-hosted VM. On a hosted Postgres instance, run
`python manage.py init` once (or set `SEED_DEMO_ON_BOOT=1`) to provision the
operator + demo data.

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
