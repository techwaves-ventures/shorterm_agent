# Web process for Render/Heroku/Fly-style platforms.
# One worker (the scrape runner keeps in-process per-tenant state + browser, so
# multiple workers would break the OTP rendezvous); threads handle concurrent
# dashboard requests. Binds the platform-provided $PORT.
web: gunicorn dashboard:app --bind 0.0.0.0:${PORT:-5050} --workers 1 --threads 8 --timeout 120

# Off-Vercel scrape worker: claims queued FurnishedFinder jobs from the shared DB
# and runs the live browser scrape. Run this on a host WITH Playwright + Chromium
# that shares the same DATABASE_URL as the web/dashboard (Vercel can't scrape).
worker: python worker.py
