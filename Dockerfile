# Runs the Flask dashboard (and, via `command:` override, worker.py) with a
# real Google Chrome for Playwright. See docker-compose.yml for how the pieces
# fit together, and DEPLOY.md for non-Docker deployment targets.
FROM python:3.12-slim

WORKDIR /app

# Playwright's own installer pulls the Chrome-required apt packages (--with-deps),
# so no manual apt-get list to maintain here.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chrome

COPY . .

# No display in a container — the app's default visible browser needs one.
ENV HEADLESS=1 \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=5050

EXPOSE 5050

# Matches the Procfile/render.yaml web process: one worker (the scrape runner
# keeps per-tenant browser/OTP state in-process; more workers would break the
# OTP rendezvous), threads handle concurrent dashboard requests.
CMD ["gunicorn", "dashboard:app", "--bind", "0.0.0.0:5050", "--workers", "1", "--threads", "8", "--timeout", "120"]
