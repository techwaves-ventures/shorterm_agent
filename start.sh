#!/usr/bin/env bash
# Start the FurnishedFinder lead dashboard.
# Creates the venv and installs deps on first run, then launches dashboard.py.
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
PY="$VENV/bin/python"

# Bootstrap the virtualenv + dependencies if missing.
if [ ! -x "$PY" ]; then
    echo "Creating virtualenv in $VENV ..."
    python3 -m venv "$VENV"
    "$PY" -m pip install --upgrade pip
    "$PY" -m pip install -r requirements.txt
    "$PY" -m playwright install chrome
fi

if [ ! -f .env ]; then
    echo "Warning: .env not found — copy .env.example to .env and fill it in." >&2
fi

host="${DASHBOARD_HOST:-127.0.0.1}"
port="${DASHBOARD_PORT:-5000}"
echo "Starting dashboard on http://${host}:${port}"
exec "$PY" dashboard.py
