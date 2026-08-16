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

**Prefer email ingestion over scraping.** Forwarding FurnishedFinder's own
notification emails to the app (see "Inbound email" in the README) reads leads
without any automated access to their site, and is the default for new tenants.
The browser path below remains only for sending replies and for hosts who
haven't set forwarding up.

`BROWSER_PROXY_SERVER` (with `BROWSER_PROXY_USERNAME` / `BROWSER_PROXY_PASSWORD`)
routes browser traffic through a proxy — for a corporate egress policy or a fixed
IP you've agreed with your own network. It now also requires `BROWSER_PROXY_ACK=1`,
because a proxy must **not** be used to evade a site's IP block: that is
circumvention of a technical access control, not automation. If FurnishedFinder
blocks the host, stop and resolve it with them. Keep proxy credentials out of
repo files.

The `Procfile` declares this as the `worker:` process; a Render/Fly worker
service or a systemd unit can run the same command. Until a worker is online the
UI says so (jobs stay *queued*), so nothing looks connected that isn't.

> #### ⚠️ Deploy the web app BEFORE the worker
>
> The web host and the worker are deployed separately, so they can run different
> revisions for a while. **Upgrade the web app first, or both together — never the
> worker first.**
>
> `ff_worker.last_seen`, `ff_jobs.created_at` and `ff_jobs.updated_at` are written
> on one host and read on the other, so they carry a UTC offset (`jobs._now_utc`,
> VEN-142). A **new worker writing to an old web app** is the bad order: the old
> reader does `datetime.now() - stamp` and raises `TypeError: can't subtract
> offset-naive and offset-aware datetimes`. That is not a degraded number, it is an
> uncaught exception on the dashboard page itself, `/api/status`, `POST /refresh`
> and `/otp` — every tenant, every page, starting within ~15s of the worker
> restarting (its heartbeat interval) and lasting until the web deploy lands.
>
> The other order is safe: a new web app reads an old worker's naive stamps exactly
> as the previous release did. The cross-host fix simply does not take effect for a
> given column until that column's writer is upgraded — and note `created_at` is
> stamped once at enqueue and never re-stamped, so jobs queued by an old web host
> keep a naive `created_at` for their whole life.
>
> **Rolling back has the same hazard, in the same direction, plus a quieter one.**
> Once a new worker has written an offset-aware `last_seen`, rolling the *web*
> host back to the previous revision re-opens exactly the `TypeError` above — the
> row is already aware and the old reader cannot subtract it. **Roll the worker
> back first, then the web host** (the reverse of the deploy order). This is the
> part reached for under pressure, so it is worth knowing before the incident.
>
> Two caveats that decide whether that actually works:
>
> - **"Worker first" assumes the worker is still running.** A rolled-back worker
>   only clears the aware `last_seen` by heartbeating a naive one over it. If the
>   worker is stopped or crashed — often *why* you are rolling back — nothing
>   re-stamps it, and the web rollback hits the `TypeError` anyway. In that case
>   clear the beacon first (`DELETE FROM ff_worker WHERE id=1;`, or wait for one
>   naive heartbeat); an empty table reads as "offline", which every revision
>   handles.
> - **The `ff_jobs` columns degrade silently rather than loudly.** The old
>   `_age_seconds` catches `Exception` broadly, so an aware `created_at` /
>   `updated_at` written by the new stack reads as `None` on the rolled-back web
>   host — no traceback, no log line. `_cooldown_remaining` then returns 0 (the
>   FurnishedFinder login-email burst guard is **off**) and the
>   `MAX_ACTIVE_JOB_SECONDS` backstop in `reap_stale` is **skipped** (a wedged job
>   is never reaped). Because `created_at` is stamped once at enqueue and never
>   re-stamped, that lasts the whole life of every job the new stack queued. It
>   clears as those jobs finish; a rollback expected to last should drain or
>   delete them.
>
> On the single-image topologies (`Dockerfile`, `docker-compose.yml`) the web and
> worker roles turn over together, so there is no mixed window and no ordering to
> enforce in either direction.
>
> There is no migration to run **going forward**: a new reader handles both
> shapes, and a legacy naive value is read as local wall clock, which is what
> wrote it. That symmetry does not hold backwards — see the two caveats above.

**Connection honesty:** saving a FurnishedFinder email lands the account in
`needs_verification` — it is **not** shown as connected. The first successful
worker scrape (a real OTP login) is what flips it to `connected`. See
`ff_account.py`.

Email sends (SMTP) already work anywhere; only the *platform*-channel send needs
the browser. Human-in-the-loop send posture is unchanged — nothing auto-sends.

This split (Vercel = dashboard/UI, `worker.py` = browser jobs, shared Postgres)
is the recommended production topology.

#### Deploy order for the worker-liveness beacon (upgrading past VEN-137)

`ff_worker.last_seen` is written by the worker and read by the web host. It now
carries a UTC offset, because comparing a naive wall-clock stamp across two
hosts is off by the offset between them — which always exceeds the 90s TTL, so
a westward worker read permanently offline while healthy, and an eastward
worker read online *even after it had crashed*.

Upgrading across that change:

- **Deploy the web host first.** It understands both the old naive stamp and
  the new offset-aware one.
- **Do not deploy the worker first.** It would write offset-aware stamps that an
  old web host subtracts from a naive `datetime`, raising an uncaught
  `TypeError` — a 500 on the dashboard `/api/status` poll. This hits *every*
  deploy, not only split-timezone ones.
- **Rolling the web host back has the same hazard** once a new worker has
  written an aware stamp. Roll the worker back first, or expect that 500 until
  the web host is forward again.
- **The fix is inert until the worker is redeployed.** A naive row persists
  until a new-code worker heartbeats; it is not self-healing within the 90s
  TTL. If the worker has crashed — exactly the case the beacon exists to catch
  — the row stays naive indefinitely.
- On the single-image topologies (`Dockerfile`, `docker-compose.yml`) both roles
  turn over together, so there is no mixed window and no ordering to enforce.

Pinning `TZ=UTC` on both hosts is cheap belt-and-braces, and is currently not
set anywhere in this repo.

### Authenticated Chrome wake endpoint on a VM

For deployments that need an HTTP wake hook in front of the browser worker,
run `chrome_task_server.py` on the browser-capable VM. It is a deliberately
narrow Flask service, not a browser automation API:

- VM bind used for direct callers: all interfaces on port `6756`
- no `/docs` or OpenAPI surface
- `GET /healthz` is signed-auth only; unauthenticated callers get 401
- `POST /v1/wake` only claims existing queued DB jobs; it never accepts URLs,
  scripts, selectors, or arbitrary commands
- every wake request requires:
  - `Authorization: Bearer <CHROME_TASK_BEARER_TOKEN>`
  - `X-Shorterm-Timestamp` Unix seconds, within the configured tolerance
  - `X-Shorterm-Nonce`, accepted once per process window
  - `X-Shorterm-Signature`, HMAC-SHA256 over
    `timestamp + "\n" + nonce + "\n" + method + "\n" + path + "\n" + sha256(body)`
- if `CHROME_TASK_BEARER_TOKEN` or `CHROME_TASK_HMAC_KEY` is missing, the
  service fails closed and will not process jobs.

Required env for the VM service, stored outside git:

```bash
CHROME_TASK_HOST=0.0.0.0
CHROME_TASK_PORT=6756
CHROME_TASK_BEARER_TOKEN=...
CHROME_TASK_HMAC_KEY=...
```

Install as a user service on this VM:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/shorterm-chrome-task-server.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now shorterm-chrome-task-server.service
```

When binding publicly, keep the same bearer+HMAC requirement at the origin and
expose only this narrow service port. Do not expose a raw browser command
endpoint.

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

> **There is still no migration to run, but one release is not schema-neutral.**
> The release that introduces `timeframe.py` changes the *frame* the schedule
> columns are written in, without changing their type or shape. On a UTC fleet
> that is a no-op. On a fleet in any other zone, read
> [Upgrading across the schedule-frame change](#upgrading-across-the-schedule-frame-change)
> **before** you deploy.

---

## Upgrading across the schedule-frame change

**Applies to:** the release that adds `timeframe.py` (VEN-134).
**If every host in your fleet runs UTC — the hosted Vercel/Render setup above —
this section is a no-op. Deploy normally.** It matters only when your web host
or worker runs in a non-UTC zone.

### What changed

`outbox.scheduled_at` and `deals.next_action_at` decide *when* an approved
message may go out. They are written by whichever process drafts or approves the
message and read by whichever process drains the queue — on the worker-queue
topology those are different hosts sharing one `DATABASE_URL`. They used to be
stored as the **writing host's local wall clock**, so a drainer in a different
zone compared them against its own clock and was wrong by the offset. They are
now stored as **naive UTC** so that every host agrees.

Both are still plain `YYYY-MM-DDTHH:MM:SS` text with no offset suffix, because
they are compared with SQL `<=` and sorted with `ORDER BY` — a lexicographic
comparison that an offset suffix would break. The column type does not change,
so there is nothing to `ALTER`.

### No deploy order is safe on a non-UTC fleet

The usual "deploy the web app first, then the worker" rule does **not** rescue
this one. Which order is harmful flips with the *sign* of your fleet's offset —
verified end-to-end, approving on one head and draining on the other against a
shared database:

| fleet zone | web=new / worker=old | web=old / worker=new |
|---|---|---|
| `America/Los_Angeles` (−7) | **approved sends stall** | **deferred sends fire early** |
| `UTC` | safe | safe |
| `Europe/Berlin` (+2) | **deferred sends fire early** | **approved sends stall** |

**Neither column is a safe choice on a non-UTC fleet** — read both cells in your
row before picking an order. The two failures are different, and the second one
is easy to mistake for success:

- **Stalled** — the operator clicks **Approve & send**, gets a success UI, and
  the message sits `queued` for up to the full offset. `reclaim_stuck_sending`
  does not rescue it: that reaper only looks at rows in `sending`, and these are
  `queued`. Nothing surfaces it.
- **Fired early** — a send deliberately deferred to a civilised hour goes out
  ahead of its scheduled time, i.e. the quiet-hours clamp is defeated and a guest
  can be messaged inside quiet hours. Queue metrics look *healthy* here, because
  messages are moving.

### What to actually do

**Cut both hosts over together, and let nothing drain on the old code while the
new code is writing.** Draining the queue first is *not* sufficient on its own —
a message approved during the mixed-code window stalls even when the queue was
completely empty when the deploy started (verified). Emptying the queue is about
the *existing* backlog; the atomic cutover is about new approvals.

Two processes drain the outbox, and only one of them is the "worker":

- **`worker.py`** — the off-host drainer (`run_agent_pass` → `automation.send_next`).
  This is the one that can be in a different zone from the web host.
- **the dashboard itself** — `automation.start_drainer` runs an in-process
  drainer thread right after an approve, so the web host delivers its own
  messages without help.

That second one is why a web-only deploy is self-consistent: the same host wrote
the stamp and read it. The hazard is specifically `worker.py` reading rows the
web host wrote (or the reverse) while the two are on different code.

```bash
# On a non-UTC fleet, in this order. Draining comes FIRST and stopping the
# drainer SECOND: the off-host drainer is the thing that empties the queue, so
# stopping it first leaves step 2 waiting on a queue that can no longer drain.

# 1. Let the queue drain on the OLD code, with the drainer still running, then
#    confirm nothing DELIVERABLE is left. Do NOT wait for a plain count of
#    queued rows to reach zero — it generally never will. `outbox.next_queued`
#    only picks up rows whose `scheduled_at` has arrived, so any send the
#    quiet-hours clamp deferred to tomorrow morning sits in `queued`,
#    undrainable on purpose, for up to ~12h. Gate on what is actually due:
#    (psql)
#      SELECT count(*) FROM outbox WHERE status='sending'
#         OR (status='queued'
#             AND scheduled_at <= to_char(now() AT TIME ZONE 'utc',
#                                         'YYYY-MM-DD"T"HH24:MI:SS'));
#    (sqlite3 "$SQLITE_PATH")
#      SELECT count(*) FROM outbox WHERE status='sending'
#         OR (status='queued'
#             AND scheduled_at <= strftime('%Y-%m-%dT%H:%M:%S','now'));
#    Future-dated `queued` rows are expected and are NOT an error. They are
#    legacy-frame rows that will outlive the deploy, which draining cannot fix
#    — the next section covers what happens to them.

# 2. Now stop the off-host drainer, so nothing reads new-frame rows with old
#    code during the cutover.
#    However you run it — a Procfile `worker` dyno, a Render/Fly worker, or a
#    bare `python worker.py` on a VM. Note that NONE of the units in deploy/
#    runs worker.py: that directory is the single-VM topology, which has no
#    separate worker host and is therefore unaffected by any of this.
#      Render/Heroku-style:  scale the `worker` process to 0
#      VM:                   stop/kill the `python worker.py` process

# 3. Deploy the new code to BOTH the web host and the worker host.

# 4. Start the worker again (scale back to 1, or restart `python worker.py`).
```

Nothing here stops the web host accepting approvals, so from step 1 until step 3
completes the queue is a moving target: each approve adds a row in whichever
frame the web host is *currently* running. Do not chase the count to a stable
zero — take the reading, and move on to step 2 promptly.

Two things make that window survivable, and it is worth being exact about which
does the work, because a single cutover on its own does **not** close it. A row
approved by the old web code before step 3 still exists after step 3 and is read
by the new-code worker at step 4 — the legacy-frame case. What saves the common
path is `automation.start_drainer` (`automation.py`): the web host runs its own
in-process drainer immediately after an approve, so a send that is due *now* is
written and delivered by the same host on the same code, and never crosses the
frame boundary at all. What the single cutover buys is the narrower guarantee
that no host is left reading one frame while another writes the other.

The rows that genuinely survive are the ones the clamp deferred — approved
during the window, not yet due, still queued at step 4. On a non-UTC fleet those
are offset by the old frame; see the next section, which is why that section
exists rather than being replaced by "just drain it first."

### Pre-existing rows are not rewritten (deliberate)

**Nothing rewrites the schedule columns into the new frame, and that is a
decision, not an oversight.** A row written before this release holds the
*writing host's* local time, and the database does not record which host wrote it
or what zone that host was in. Any backfill would have to guess an offset and
apply it to every row; guessing wrong silently moves real customer sends, which
is worse than the bounded problem it would fix.

One at-rest sweep does exist and is easy to mistake for a backfill:
`pipeline._normalize_legacy_timestamps` runs on the first query in every process
and rewrites deal timestamps that are still in the database's space-separated
`YYYY-MM-DD HH:MM:SS` form, converting them UTC→**host-local**. `next_action_at`
is in its column list, so it is worth being precise about why this is not a
frame problem:

- The sweep only touches *space-separated* values, and every schedule stamp this
  release writes is `isoformat()` — `"T"`-separated. Those pass through
  `pipeline.norm_ts` byte-identical, which is pinned by
  `test_norm_ts_leaves_schedule_frame_stamps_alone`.
- `next_action_at` has no database default (see the `deals` DDL) and is only ever
  written by application code, so in practice it is never space-separated.

The residual inconsistency is a comment, not behaviour: `_TS_COLS` still
describes the canonical shape for those columns as "local time", which is no
longer true of `next_action_at` specifically. If a space-separated
`next_action_at` ever did appear, that sweep would convert it to host-local —
i.e. out of the schedule frame.

So on a **non-UTC** fleet, rows already in the queue at deploy time are read one
offset out for the rest of their (short) life:

- **West of UTC** every legacy schedule up to `|offset|` hours in the future
  becomes due immediately and fires early.
- **East of UTC** a legacy row that is genuinely due is withheld for `|offset|`
  hours.
- Mixed frames also mis-sort `ORDER BY scheduled_at`, so the drainer can pick a
  legacy row ahead of a correctly-framed one.
- `automation.run_due` copies a deal's existing `next_action_at` straight into
  the new outbox row it creates (`automation.py`, the `outbox.add(...)` call with
  `scheduled_at=deal.get("next_action_at")`), so a legacy stamp can survive one
  extra hop after the upgrade.

This is bounded and self-healing — every row is rewritten in the new frame the
next time it is scheduled — and step 1 above (drain the queue before cutting
over) avoids it entirely. **On a UTC fleet none of it applies:** the old local
frame and the new UTC frame are the same values.

### One display change ships with this

The dashboard now renders schedule times in the **property's** timezone
(`Settings → timezone`) rather than the server's. Existing deals will show a
different clock time after this deploy **with no change to the underlying data** —
the new number is the correct property-local time, and the old one was the
server's. If the tenant has no timezone configured, nothing changes.

One consequence is visible and worth expecting: because the quiet-hours clamp
still computes in the *server's* zone, a send the system clamped to 08:00 can
display as e.g. `01:00` in the property's zone. That display is telling the
truth — the send really is scheduled for 01:00 where the guest is. Moving the
clamp itself into the property zone is tracked separately (**VEN-141**); until
that lands, treat an out-of-hours time on the dashboard as a real finding rather
than a rendering bug.

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
