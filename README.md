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

**On Linux**, two things differ from macOS:

- Install Chrome's system libraries too: `playwright install --with-deps chrome`
  (plain `playwright install chrome` leaves it missing shared libs like
  `libnss3`/`libatk` that macOS gets from the OS, so the browser fails to launch).
- Set `HEADLESS=1` in `.env` unless you're on a machine with a real display —
  the default visible browser needs an X server (`$DISPLAY`) to render into, and
  a headless server/VM has none. `--no-sandbox` is already applied in the launch
  args, which is the other thing Linux (root/containers especially) usually needs.

See [DEPLOY.md](DEPLOY.md) for the full Ubuntu VM deployment (systemd service +
timer) if you're setting up a server, not just developing locally.

## Docker

```bash
cp .env.example .env   # fill in SECRET_KEY / FF_CRED_KEY / OPERATOR_EMAIL / OPERATOR_PASSWORD
docker compose up --build
```

Starts the dashboard at <http://127.0.0.1:5050> — SQLite persisted to `./data/`,
the FurnishedFinder browser session persisted to a named volume so you're not
re-OTP'd on every restart. The image runs headless (containers have no display)
and forces `linux/amd64`: real Google Chrome (the "chrome" channel this app
drives, not the Playwright-bundled Chromium) only ships for Linux x86_64, so on
Apple Silicon this builds/runs under emulation — slower locally, but it's the
same architecture as most actual Linux hosts, so nothing changes for deployment.

To try the production worker-queue split (dashboard enqueues scrape jobs,
`worker.py` drains them against shared Postgres — see DEPLOY.md) instead of
in-process scraping:

```bash
# .env also needs: DATABASE_URL=postgresql://shorterm:shorterm@postgres:5432/shorterm
#                   FORCE_WORKER_QUEUE=1
docker compose --profile worker up --build
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

## The guest lifecycle (deals + agentic sequences)

The dashboard is organized around **deals**, not an inbox. Every scraped lead or
message opens a deal that moves through stages and carries a *next action due at
a time*:

```
new → contacted → nurturing → booked → pre_arrival → staying → completed
                            ↘ lost
```

Two playbooks drive it (`sequences.py`):

- **Pre-booking nurture** — first reply, then follow-ups at +2d / +5d / +10d of
  silence. Most deals die of silence rather than a bad first message, so this is
  where the agent earns its keep.
- **Booked → arrival** — confirmation, the one-week-before note, check-in
  instructions, arrival-day welcome, mid-stay check-in, and checkout details,
  all anchored to the guest's check-in/check-out dates.

`automation.py` drafts whatever is due and puts it in the **outbox**
(`outbox.py`), which is the single gate between "the agent wrote it" and "the
guest got it". The dashboard shows what's scheduled, what's awaiting approval,
and what's been sent.

### How leads get in: forwarded email (default) or scraping

**Email is the default and the recommended path.** FurnishedFinder emails the
host whenever a lead or message arrives. Forwarding that email to Shorterm means
we read leads **without any automated access to FurnishedFinder's site** — no
scheduled scraping, no bot-detection surface, no circumvention question — and
leads arrive the moment they're sent rather than at the next scheduled check.

Each tenant gets an unguessable address, shown in **Settings → How leads reach
us**:

```
leads+7-a1b2c3d4e5f6@inbound.yourdomain.com
```

The host adds a forwarding rule (Gmail: Settings → Forwarding, plus a filter for
`furnishedfinder.com`). Point your mail provider's inbound webhook at
`POST /inbound/email` and set `INBOUND_EMAIL_DOMAIN` + `INBOUND_WEBHOOK_SECRET`.

This endpoint is public and unauthenticated by nature, so it applies four
independent checks — a forged "lead" would otherwise be drafted at and possibly
auto-replied to:

1. **Unguessable address** — the suffix is an HMAC of the tenant id under
   `SECRET_KEY`, compared in constant time. One tenant's address reveals nothing
   about another's.
2. **Provider webhook secret** — checked before the body is examined. Fails
   closed when unset.
3. **Sender allowlist** — the message must genuinely originate from a
   FurnishedFinder domain.
4. **Size cap** — oversized payloads are dropped before parsing.

Every outcome returns `202`, so a prober can't discover which tenants or
addresses exist. Anything that fails a check is dropped and logged; a message
that can't be parsed confidently is dropped rather than becoming a half-empty
lead the agent would write to.

> **Scheduled checks** remain available (Settings → *Scheduled checks*) for hosts
> who can't set up forwarding, and the browser is still used to *send*
> on-platform replies. But when a tenant is on email ingestion, the scheduler
> refuses to run browser checks for them at all — the enforcement lives in
> `scheduler.due_slot`, not just in the UI.
>
> The browser also no longer conceals that it's automated: we removed
> `--disable-blink-features=AutomationControlled` and the `--enable-automation`
> strip, and a proxy now requires an explicit `BROWSER_PROXY_ACK=1`. Hiding
> automation from a site's detection is circumvention of a technical access
> control, which is a materially worse posture than "a tool acting for a
> consenting user". Fidelity note: an email notification carries fewer facts
> than the detail-page scrape, so items ingested this way are marked
> `source: "email"`.

### Autopilot

One switch (**Automations → Autopilot**, off by default) grants the system real
autonomy:

- **Checks FurnishedFinder on a schedule** — twice a day by default (09:00 and
  16:00 local, editable). Slots only fire inside business hours, fire at most
  once each per day, and a slot missed because the machine was asleep still runs
  later that day rather than being skipped.
- **Replies to good-fit leads immediately** — the draft goes straight to the
  send queue instead of waiting for you to open the app. These leads go to
  several landlords at once and the first genuine reply usually wins, so the
  delay *is* the lost booking.

**Skipping is deliberately rare.** The agent's default is to reply. It skips
only on a hard, explicit mismatch (stated budget below a set price, travelers
over a set occupancy, pets where pets are barred, a stay outside min/max nights,
or a message that isn't from a prospective tenant). Anything unknown, blank, `0`
or ambiguous is a question to ask in the reply — not a rejection.

> **It never skips on location.** A lead's `Location:` field is where the tenant
> *currently lives*, not where they want to rent — the destination is already
> settled, because they inquired about a specific listing. Treating that as a
> mismatch rejects exactly the relocation tenants this business runs on.

### Daily summary email

One email at the end of each day (18:00 **property-local** by default) covering
what the agent sent, who is still waiting on a reply, anything that failed, and
arrivals in the next week. Set the property's timezone in **Settings** — every
schedule follows that clock, not the server's, so a DC host gets 6pm Eastern
even when the app runs in UTC.

On a quiet day nothing is sent. An empty daily email trains people to ignore the
channel, and this is the one place the product reaches an owner who isn't logged
in — so it stays worth opening.

### Email delivery (Resend or SMTP)

Set `RESEND_API_KEY` + `RESEND_FROM` to use [Resend](https://resend.com)
(preferred), or the `SMTP_*` vars for a plain relay. Resend wins when both are
present; with neither, email features degrade quietly instead of erroring.

**Guest replies never send *as* the host's own address.** We aren't authorised
to send as their Gmail, so putting it in `From:` fails SPF/DKIM and lands the
message in spam — Resend rejects it outright. Instead the host's identity rides
in the display name and `Reply-To`:

```
From:     Sagiv via Shorterm <hello@yourdomain.com>   ← verified, authenticates
Reply-To: sagiv@gmail.com                             ← replies reach the host
```

The guest sees the host's name and replying goes straight to them. Verify your
sending domain in Resend first, and make `RESEND_FROM` an address on it.

> Gmail SMTP caps around 500 messages/day and flags bulk automation — fine for a
> pilot, not for production guest email.

### Account security

- **CSRF protection** on every state-changing form and `fetch()` — without it,
  any page a logged-in host visited could make their browser send replies to
  guests or disconnect their FurnishedFinder account.
- **Session cookies** are `HttpOnly` + `SameSite=Lax` + `Secure` (set
  `INSECURE_COOKIES=1` only for local http development).
- **Login throttling**: an account locks for 15 minutes after 8 consecutive
  failures. Per-account rather than per-IP, so a shared office NAT can't lock
  out a whole team.
- **Password policy**: 10 characters minimum, with common and low-entropy
  passwords rejected.
- **Password reset** via a signed, one-hour link. Tokens are single-use without
  a tokens table — the current password hash is folded into the signature, so
  changing the password invalidates every outstanding link.
- **Stripe webhooks fail closed**: a missing `STRIPE_WEBHOOK_SECRET` returns 503
  rather than trusting unsigned events (which would let anyone grant themselves
  a subscription).

### Safety model — three layers

1. **Off by default.** Automation is per-tenant and starts disabled: the agent
   drafts and schedules everything, but every message waits for your approval.
2. **Per-step opt-in.** With automation on, only the steps you tick in
   **Automations** send unattended. The whole pre-sale sequence defaults to
   *off* — a weak first impression to a live prospect is expensive and hard to
   undo. Pre-arrival logistics default to *on*, where the value is highest and
   the risk lowest.
3. **Hard locks.** Check-in instructions are `never_auto` and can't be armed by
   any setting, because they carry access details — a hallucinated door code
   locks a real guest out of a real house. The drafting prompt also forbids
   inventing codes, addresses, or prices, and writes `[ADD ACCESS DETAILS]`
   instead when a fact is missing from the unit catalog.

Marking a deal **booked** is an explicit one-click action: booking happens
off-platform (a lease, a call), so it can't be scraped honestly.

**Sending is asynchronous.** Clicking send only queues the message (a few ms);
a background drainer drives the browser and the card tracks the outcome, so you
can keep working or close the tab. A failed send is recorded against the message
with its error, shown on the card with a **Retry send** button, and pushed to
your notification webhook — it is never silently dropped or mis-recorded as sent.

### Setting up your properties

**Settings** is a plain form — no JSON. Each property has labelled fields
(name, area, monthly price, max guests, pets, min/max nights, and free-text
notes the agent is allowed to quote). Blank means *unknown*: the agent asks the
guest instead of guessing, and never quotes a price you didn't enter.

Properties are also **discovered from your leads** — every FurnishedFinder lead
names the listing it came from, so Settings offers a one-click import of any
property it sees that isn't in your catalog yet. Only the name and area are
imported (the facts FF actually gave us); price and house rules stay for you to
fill in, because the agent states those to real guests.

Run the agent on demand from the dashboard (**Run agent now**), or let
`worker.py` scan every `AGENT_INTERVAL_SECONDS` (default 300) on a
browser-capable host.

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
