# Deploying the demo SaaS (Render / Fly / any web host)

The web app is a standard WSGI Flask app (`dashboard:app`) served by gunicorn.
A `Procfile` and `render.yaml` blueprint are included.

## Required environment variables

| Var | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | **yes** | Signs login session cookies. App won't start without it. |
| `FF_CRED_KEY` | **yes** | Fernet key encrypting tenants' FurnishedFinder email at rest. |
| `OPERATOR_EMAIL` / `OPERATOR_PASSWORD` | recommended | First admin login, created on boot. |
| `ANTHROPIC_API_KEY` | for drafting | Enables the auto-responder. |
| `SQLITE_PATH` | for persistence | Point at a mounted disk (e.g. `/var/data/leads.db`) so data survives deploys. |
| `PUBLIC_BASE_URL` | for Stripe | Public URL used in Stripe redirect callbacks. |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_STARTER`, `STRIPE_PRICE_PRO` | optional | Enable live billing. **Omit all to run billing in safe demo mode.** |
| `DASHBOARD_HOST=0.0.0.0` | on a host | Bind all interfaces (platform terminates TLS in front). |

Generate the two keys:

```bash
python -c "import secrets; print(secrets.token_hex(32))"                                  # SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # FF_CRED_KEY
```

Never commit secrets. `.env` is gitignored; on a host set them in the dashboard.

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

## Storage: SQLite now, Postgres later

The demo uses SQLite (`SQLITE_PATH`) on a persistent disk — simple and adequate
for a pilot/demo. **This is not a full ORM swap.** For multi-instance scale,
migrate to managed Postgres: the schema is small and centralized (`storage.py`,
`models.py`, `config.py`, `ff_account.py`, `billing.py`, `waitlist.py` each own
their `CREATE TABLE`), and all access goes through per-module `_conn()` helpers,
so the migration is: (1) add a `DATABASE_URL` branch in each `_conn()` that
returns a `psycopg` connection, (2) swap `?` placeholders for `%s`, (3) run the
same `CREATE TABLE` DDL on Postgres. Tracked as a follow-up — see the PR notes.

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
