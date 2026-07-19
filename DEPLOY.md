# Deploying the demo SaaS

The web app is a standard WSGI Flask app (`dashboard:app`). It runs two ways:

- **Vercel serverless** (fastest hosted demo) — `api/index.py` + `vercel.json`,
  with a hosted Postgres `DATABASE_URL`. See **[Deploy on Vercel](#deploy-on-vercel-hosted-postgres)** below.
- **Disk-based host** (Render / Fly / a VM) — gunicorn via `Procfile` /
  `render.yaml`, using SQLite on a persistent disk *or* the same hosted Postgres.

The database backend is auto-selected: set `DATABASE_URL` → Postgres; otherwise
SQLite at `SQLITE_PATH`. Tenant isolation is identical on both (every row is
scoped by `tenant_id`).

## Required environment variables

| Var | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | **yes** | Signs login session cookies. App won't start without it. |
| `FF_CRED_KEY` | **yes** | Fernet key encrypting tenants' FurnishedFinder email at rest. |
| `DATABASE_URL` | **yes on serverless** | Hosted Postgres (Neon / Vercel Postgres / Supabase). Required on Vercel — SQLite can't persist there. Omit to use SQLite. |
| `OPERATOR_EMAIL` / `OPERATOR_PASSWORD` | recommended | First admin login, created on boot. |
| `ANTHROPIC_API_KEY` | for drafting | Enables the auto-responder. |
| `SQLITE_PATH` | SQLite hosts only | Point at a mounted disk (e.g. `/var/data/leads.db`). Ignored when `DATABASE_URL` is set. |
| `SEED_DEMO_ON_BOOT=1` | optional | Auto-seed the demo tenant on first boot (handy on Vercel). Idempotent. |
| `PUBLIC_BASE_URL` | for Stripe | Public URL used in Stripe redirect callbacks (e.g. `https://<app>.vercel.app`). |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_STARTER`, `STRIPE_PRICE_PRO` | optional | Enable live billing. **Omit all to run billing in safe demo mode.** |
| `DASHBOARD_HOST=0.0.0.0` | disk hosts only | Bind all interfaces (not used on Vercel). |

Generate the two keys:

```bash
python -c "import secrets; print(secrets.token_hex(32))"                                  # SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # FF_CRED_KEY
```

Never commit secrets. `.env` is gitignored; on a host set them in the dashboard.

## Deploy on Vercel (hosted Postgres)

Vercel runs the Flask app as a Python serverless function (`api/index.py`,
wired by `vercel.json`). Because serverless has **no persistent filesystem**, it
needs a hosted Postgres database — do that first.

### 1. Create a hosted Postgres instance

Pick one (all have free tiers). You need the connection string.

- **Vercel Postgres** — in your Vercel project: **Storage → Create Database →
  Postgres**. Vercel auto-adds `POSTGRES_URL` / `DATABASE_URL` to the project's
  env. Copy the pooled connection string.
- **Neon** ([neon.tech](https://neon.tech)) — create a project, copy the
  connection string from the dashboard (use the **pooled** one). It looks like
  `postgresql://user:pass@ep-xxx-pooler.region.aws.neon.tech/neondb?sslmode=require`.
- **Supabase** ([supabase.com](https://supabase.com)) — **Project Settings →
  Database → Connection string → URI** (use the connection **pooler** URI, port
  6543). Add `?sslmode=require`.

Keep `sslmode=require` in the URL for all three.

### 2. Create the Vercel project

1. Push this repo/branch to GitHub.
2. In Vercel: **Add New… → Project**, import the repo. Framework preset: **Other**
   (the included `vercel.json` handles the build). No build/output overrides needed.
3. **Environment Variables** — add:
   - `DATABASE_URL` = your Postgres URL from step 1 (if you used Vercel Postgres
     it may already be present).
   - `SECRET_KEY` = `python -c "import secrets; print(secrets.token_hex(32))"`
   - `FF_CRED_KEY` = `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   - `OPERATOR_EMAIL`, `OPERATOR_PASSWORD` — your first admin login.
   - `SEED_DEMO_ON_BOOT=1` — to populate the demo tenant automatically on first boot.
   - *(optional)* `ANTHROPIC_API_KEY`, `PUBLIC_BASE_URL=https://<app>.vercel.app`,
     `STRIPE_*`. Omit Stripe to stay in demo billing mode.
4. **Deploy.** Vercel installs the slim `api/requirements.txt` (no Playwright)
   and serves every route through `api/index.py`.

### 3. Seed operator + demo data

- With `SEED_DEMO_ON_BOOT=1`, the first request provisions the operator tenant
  and the demo tenant automatically (idempotent).
- Or run the one-shot bootstrap locally against the hosted DB:

  ```bash
  DATABASE_URL='postgres://…?sslmode=require' \
  SECRET_KEY=x FF_CRED_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
  OPERATOR_EMAIL=you@example.com OPERATOR_PASSWORD=… \
  python manage.py init
  ```

  `manage.py init` prints the demo login (`demo@shorterm.test` /
  `demo-shorterm-2026` by default).

### 4. Verify

- `GET https://<app>.vercel.app/healthz` → `{"status":"ok","db":true,...}` (200).
  `db:false` (503) means the app booted but can't reach Postgres — check
  `DATABASE_URL` / `sslmode`.
- Demo path: landing `/` → **Log in** → demo dashboard with seeded leads/drafts →
  Settings / Billing (demo mode).

### ⚠️ Vercel can't run the browser — scraping is worker-backed via the shared DB

Live FurnishedFinder scraping and platform sends use Playwright + a real Chrome,
which **cannot run on Vercel serverless** (no browser, read-only FS, short
execution limits). So Vercel never runs Playwright in-process. Instead:

- **"Check now" on Vercel enqueues a job** in the shared Postgres (`ff_jobs`,
  see `jobs.py`) rather than scraping inline. The dashboard shows an honest
  status — *queued*, *worker offline*, *waiting for your one-time code*,
  *checking*, *done*, or a friendly *failed* — never a raw runtime/stack error.
- **A worker on a browser-capable host drains that queue.** Run `worker.py` on
  any host that has Playwright + Chromium and shares the **same** `DATABASE_URL`
  as the Vercel app. It claims queued jobs, runs the live scrape, writes leads
  into the shared Postgres (so the Vercel dashboard renders them), and bridges
  the tenant's OTP back through the DB (encrypted, consumed once, never logged).

```bash
# On a small always-on VM / Render worker / Fly machine (NOT Vercel):
pip install -r requirements.txt
playwright install --with-deps chrome
DATABASE_URL='postgres://…?sslmode=require' \
  SECRET_KEY=… FF_CRED_KEY=… \
  python worker.py            # polls forever; --once drains the queue and exits
```

The `Procfile` declares this as the `worker:` process; a Render/Fly worker
service or a systemd unit can run the same command. Until a worker is online the
UI says so (jobs stay *queued*), so nothing looks connected that isn't.

**Connection honesty:** saving a FurnishedFinder email lands the account in
`needs_verification` — it is **not** shown as connected. The first successful
worker scrape (a real OTP login) is what flips it to `connected`. See
`ff_account.py`.

Email sends (SMTP) already work anywhere; only the *platform*-channel send needs
the browser. Human-in-the-loop send posture is unchanged — nothing auto-sends.

This split (Vercel = dashboard/UI, `worker.py` = browser jobs, shared Postgres)
is the recommended production topology.

## Deploy on Render (blueprint)

1. Push this repo to GitHub.
2. In Render: **New + → Blueprint**, select the repo. `render.yaml` provisions a
   web service with a 1 GB persistent disk mounted at `/var/data` and
   `SQLITE_PATH=/var/data/leads.db`.
3. Fill the `sync: false` secrets (SECRET_KEY, FF_CRED_KEY, operator creds, etc.)
   in the Render dashboard.
4. Health check is wired to **`/healthz`**.
5. First boot creates the operator. To seed a demo tenant, open a shell and run
   `python manage.py seed-demo`.
6. **Stripe (optional):** add the `STRIPE_*` vars, then point a Stripe webhook at
   `https://<your-app>/billing/webhook` (events: `checkout.session.completed`,
   `customer.subscription.updated`, `customer.subscription.deleted`).

The start command runs **one** gunicorn worker (`--workers 1 --threads 8`): the
scrape runner keeps per-tenant browser/OTP state in-process, so multiple workers
would break the OTP rendezvous. Scale by running more units, not more workers.

## Storage: SQLite or hosted Postgres

The backend is chosen at runtime by `db.py`:

- **`DATABASE_URL` set** → hosted Postgres (via `psycopg`). Required on serverless
  (Vercel), recommended for any multi-instance host.
- **`DATABASE_URL` blank** → SQLite at `SQLITE_PATH` (or `./leads.db`). Great for
  local dev and single-instance disk hosts.

Every module speaks one SQLite-flavoured SQL dialect and opens connections
through `db.connect()`; when `DATABASE_URL` points at Postgres, `db.py`
translates statements (placeholders + a few DDL tokens) and provides the handful
of cross-dialect helpers that genuinely differ (`table_columns`,
`insert_returning_id`, `sync_serial`). Schema and tenant isolation are identical
on both engines — no separate migration files to run; `CREATE TABLE IF NOT
EXISTS` bootstraps a fresh Postgres database on first connect. Point a worker and
the web app at the **same** `DATABASE_URL` to share data.

---

# Deploying on an Ubuntu VM (self-hosted, single-tenant)

Runs the dashboard as a systemd service and checks for new leads/messages
twice a day (08:00 & 18:00) via a systemd timer. New leads are auto-drafted on
each check; **sending stays one-click** from the dashboard (platform + email).

## 1. Install

```bash
sudo mkdir -p /opt/str_leads
sudo chown "$USER" /opt/str_leads
git clone <your-repo> /opt/str_leads
cd /opt/str_leads

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# Chrome + its system libraries for headless Playwright:
.venv/bin/playwright install --with-deps chrome
```

## 2. Configure

```bash
cp .env.example .env
nano .env   # fill in the values below
```

Required / relevant for the VM:

- `FF_USERNAME` — your FurnishedFinder email (OTP login).
- `ANTHROPIC_API_KEY` — enables auto-drafting.
- `HEADLESS=1` — already forced by the service unit; set here too for manual runs.
- `DASHBOARD_HOST=127.0.0.1`, `DASHBOARD_PORT=5000` — keep loopback-only and reach
  it via an SSH tunnel (the dashboard has **no authentication**).
- Email channel: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `FROM_EMAIL`.
  Gmail needs an **App Password** (not your account password).
- `REPLY_CHANNELS=platform,email` — drop `email` to send on-platform only.
- `NOTIFY_WEBHOOK_URL` — Slack/Discord webhook; you get pinged when an OTP is
  needed or an email send fails. Strongly recommended for unattended runs.

Then fill `units.json` with your real unit details.

## 3. First login (seed the session)

The persistent profile in `browser_profile/` keeps you logged in between runs;
FurnishedFinder only forces a fresh OTP occasionally. Seed it once:

```bash
# from your laptop, tunnel the dashboard port:
ssh -L 5000:127.0.0.1:5000 user@your-vm

# on the VM, start the dashboard once in the foreground:
cd /opt/str_leads && HEADLESS=1 .venv/bin/python dashboard.py
```

Open <http://127.0.0.1:5000> in your laptop browser, click **Check now**. When
it shows *waiting for OTP*, check your email and paste the code. Once it
completes, stop it (Ctrl-C) and install the services below.

## 4. Install the services & timer

```bash
sudo cp deploy/str-leads-dashboard.service /etc/systemd/system/
sudo cp deploy/str-leads-check.service     /etc/systemd/system/
sudo cp deploy/str-leads-check.timer       /etc/systemd/system/
sudo systemctl daemon-reload

sudo systemctl enable --now str-leads-dashboard.service
sudo systemctl enable --now str-leads-check.timer
```

> The units assume `/opt/str_leads` and run as root. To run as your user, add
> `User=<you>` under `[Service]` in both `.service` files before copying, and
> make sure `browser_profile/` and `leads.db` are owned by that user.

## 5. Verify

```bash
systemctl status str-leads-dashboard.service
systemctl list-timers str-leads-check.timer     # shows next 08:00/18:00 fire
sudo systemctl start str-leads-check.service     # trigger a check now
journalctl -u str-leads-dashboard.service -f     # watch scrape + draft logs
```

Tunnel in (`ssh -L 5000:127.0.0.1:5000 user@your-vm`), open the dashboard,
review drafts, and click **Send**.

## Operating notes

- **OTP during a scheduled run:** the run waits up to 10 minutes for a code. You
  get a notification (if `NOTIFY_WEBHOOK_URL` is set); open the dashboard and
  paste the OTP into the still-waiting run. If it times out, just trigger
  `str-leads-check.service` again after submitting.
- **Reboots:** the dashboard restarts (`Restart=on-failure`) and the timer is
  `Persistent=true`, so a missed check fires on next boot.
- **Changing the schedule:** edit `OnCalendar` in the timer, then
  `sudo systemctl daemon-reload && sudo systemctl restart str-leads-check.timer`.
