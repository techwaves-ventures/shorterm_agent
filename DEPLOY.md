# Deploying on an Ubuntu VM

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
