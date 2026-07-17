"""Pilot waitlist capture for the public landing page.

Stores pilot-access requests (email + market + unit count) in the shared
leads.db so the landing CTA is functional in the demo without a CRM. No auth —
it's a public form — so inputs are treated as untrusted and length-capped.
"""
from datetime import datetime

import db


def _conn() -> db.Conn:
    c = db.connect()
    c.execute(
        """CREATE TABLE IF NOT EXISTS waitlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            market TEXT,
            units TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    return c


def add(email: str, market: str = "", units: str = "") -> None:
    """Record a pilot request. Raises ValueError on a blank/oversized email."""
    email = (email or "").strip()[:200]
    if not email or "@" not in email:
        raise ValueError("A valid email is required.")
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        c.execute(
            "INSERT INTO waitlist (email, market, units, created_at) VALUES (?,?,?,?)",
            (email, (market or "").strip()[:120], (units or "").strip()[:20], now),
        )


def count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM waitlist").fetchone()[0]
