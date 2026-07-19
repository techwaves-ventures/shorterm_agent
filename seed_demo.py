"""Seed a self-contained demo tenant so the SaaS flow can be shown without real PII.

Creates a demo host account, two sample units, and a handful of fake leads and
messages with drafted replies already waiting — so login → dashboard shows a
realistic, populated inbox. All names/emails are obviously fake (example.test).

    python seed_demo.py                 # create/refresh the demo tenant
    python manage.py seed-demo          # same, via the admin CLI

Prints the demo login at the end. Safe to re-run: it resets the demo tenant's
sample data each time and never touches other tenants or the operator.
"""
import json
import os

from dotenv import load_dotenv

load_dotenv()

import billing
import config
import crypto
import ff_account
import models
import storage

DEMO_EMAIL = os.getenv("DEMO_EMAIL", "demo@shorterm.test")
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "demo-shorterm-2026")
SITE = "furnishedfinder"

DEMO_UNITS = [
    {
        "id": "unit-1", "name": "Sunny 1BR near Metro", "area": "Washington DC NW",
        "monthly_price": 2400, "max_occupancy": 2, "pets_allowed": True,
        "min_nights": 30, "notes": "Furnished, utilities included, in-unit laundry.",
    },
    {
        "id": "unit-2", "name": "Quiet Studio by the Hospital", "area": "Washington DC NE",
        "monthly_price": 1800, "max_occupancy": 1, "pets_allowed": False,
        "min_nights": 30, "notes": "5 min to the medical center, desk + fast wifi.",
    },
]

# Fake inquiries — no real tenant PII. traveler/sender are invented.
DEMO_LEADS = [
    {
        "id": "lead-1001", "traveler": "Jordan (demo)", "received": "2 hours ago",
        "title": "Interested in your DC listing",
        "move_in": "Aug 15", "move_out": "Nov 14", "nights": 91, "occupants": 1,
        "pets": "None", "budget": "$2,300/mo", "detail": "Travel nurse, 13-week contract at a NW hospital.",
    },
    {
        "id": "lead-1002", "traveler": "Priya (demo)", "received": "5 hours ago",
        "title": "Furnished stay for relocation",
        "move_in": "Sep 1", "move_out": "Feb 28", "nights": 180, "occupants": 2,
        "pets": "1 small dog", "budget": "$2,600/mo", "detail": "Relocating for a new job, partner + small dog.",
    },
    {
        "id": "lead-1003", "traveler": "Marketing Co (demo)", "received": "yesterday",
        "title": "Partnership opportunity",
        "detail": "Generic sales pitch — not a real housing inquiry.",
    },
]

DEMO_MESSAGES = [
    {
        "id": "msg-2001", "sender": "Dana (demo)", "date": "1 hour ago",
        "title": "Is parking included?",
        "body": "Hi! Loved the photos. Is there off-street parking, and could I move in a few days early?",
        "move_in": "Aug 20", "nights": 60, "occupants": 1,
    },
]


def _reset_sample_data(tenant_id: str) -> None:
    """Clear this tenant's seen/response rows so re-seeding is idempotent.

    Uses storage._conn so the tables are created/migrated if they don't exist yet.
    """
    with storage._conn() as c:
        c.execute("DELETE FROM seen WHERE tenant_id=? AND site=?", (tenant_id, SITE))
        c.execute("DELETE FROM responses WHERE tenant_id=? AND site=?", (tenant_id, SITE))


def _seed_items(tenant_id: str) -> None:
    storage.filter_new(tenant_id, SITE, "lead", DEMO_LEADS)
    storage.filter_new(tenant_id, SITE, "message", DEMO_MESSAGES)

    # Drafted replies already waiting for one-click approval.
    storage.save_response(
        tenant_id, SITE, "lead", "lead-1001", status="draft", unit_id="unit-1",
        confidence="high", reason="Dates + budget + occupancy fit Sunny 1BR.",
        draft=("Hi Jordan, thanks for your interest! My Sunny 1BR in DC NW would be a "
               "great fit for a 13-week contract — it's furnished with utilities and "
               "in-unit laundry, and comfortably fits one. Happy to hop on a quick call "
               "or set up a tour. What dates work best for you?\n\nBest,\nJamie"),
        tenant_email="jordan@example.test",
    )
    storage.save_response(
        tenant_id, SITE, "lead", "lead-1002", status="draft", unit_id="unit-1",
        confidence="medium", reason="Fits occupancy/pets; confirm 6-month availability.",
        draft=("Hi Priya, congrats on the move! The Sunny 1BR in DC NW is pet-friendly "
               "and furnished with utilities included — a comfortable home base for you "
               "and your partner. Could you share your ideal move-in date so I can "
               "confirm availability through February?\n\nBest,\nJamie"),
        tenant_email="priya@example.test",
    )
    storage.save_response(
        tenant_id, SITE, "lead", "lead-1003", status="skipped",
        reason="Not a housing inquiry — looks like a sales pitch.",
    )
    storage.save_response(
        tenant_id, SITE, "message", "msg-2001", status="draft", unit_id="unit-1",
        confidence="high", reason="Direct question about the Sunny 1BR.",
        draft=("Hi Dana, glad the photos caught your eye! The Sunny 1BR includes "
               "in-unit laundry, and I can be flexible on an early move-in around Aug 20. "
               "There's convenient street parking nearby. Want to set up a quick tour?"
               "\n\nBest,\nJamie"),
        tenant_email="dana@example.test",
    )


def seed_demo() -> tuple[str, str]:
    """Create/refresh the demo tenant. Returns (email, tenant_id)."""
    models.ensure_operator()  # make sure auth tables exist
    user = models.get_user_by_email(DEMO_EMAIL)
    if not user:
        user = models.create_user(DEMO_EMAIL, DEMO_PASSWORD, tenant_name="Demo Host")
    tenant_id = user.tenant_id

    config.save_settings(
        tenant_id,
        host_name="Jamie",
        from_email="jamie@example.test",
        units_json=json.dumps(DEMO_UNITS),
        reply_channels="platform,email",
    )
    config.mark_onboarded(tenant_id)
    billing.set_subscription(tenant_id, plan="pro", status="active", demo=1)

    # Connect a fake FF account so the seeded leads/drafts are visible on the
    # dashboard (the leads view unlocks once an account is linked). This is
    # illustrative demo data — no real FF session exists — so we also mark it
    # verified to present the demo cleanly. Requires FF_CRED_KEY; skipped
    # gracefully if encryption isn't configured.
    if crypto.available() and not ff_account.has_account(tenant_id):
        try:
            ff_account.connect(tenant_id, "demo-host@example.test")
            ff_account.mark_state(tenant_id, ff_account.CONNECTED)
        except (ValueError, RuntimeError):
            pass

    _reset_sample_data(tenant_id)
    _seed_items(tenant_id)
    return DEMO_EMAIL, tenant_id


if __name__ == "__main__":
    email, tid = seed_demo()
    print(f"Demo tenant ready: {email} (tenant {tid})")
    print(f"  Password: {DEMO_PASSWORD}")
    print("  Log in and you'll see sample leads/messages with drafts waiting.")
