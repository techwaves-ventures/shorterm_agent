"""Vercel serverless entrypoint for the Shorterm Agent SaaS dashboard.

Vercel's @vercel/python builder imports this module and serves the module-level
`app` (the WSGI Flask application). The repo root is added to sys.path so the
app's modules (dashboard, db, models, ...) resolve when the function executes
from the ./api directory.

Limitations on Vercel serverless (documented in DEPLOY.md):
- No browser + read-only filesystem, so live FurnishedFinder scraping/sending is
  disabled here. The Playwright import is optional; scrape routes raise a clear
  error. Route background scraping to a worker/cron (see DEPLOY.md).
- SQLite can't persist across invocations, so a hosted Postgres DATABASE_URL is
  required (Neon / Vercel Postgres / Supabase).
"""
import sys
from pathlib import Path

# Ensure the repo root (parent of ./api) is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import app  # noqa: E402 — sys.path is configured above

# Vercel serves the module-level `app`; expose `application` too for WSGI hosts.
application = app
