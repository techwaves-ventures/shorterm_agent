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
    {
        # Dana's second message. Before the id-collision fix this one hashed
        # identically to the first and was silently dropped, so the demo could
        # never show a conversation with more than one guest turn in it.
        "id": "msg-2002", "sender": "Dana (demo)", "date": "20 minutes ago",
        "title": "Re: Is parking included?",
        "body": "That works for me — could you send the lease over? I can put a deposit down this week.",
    },
]

# Conversations that exercise the rest of the lifecycle. Without these the inbox
# can only be reviewed in the three states a fresh scrape produces, and the
# filters that matter most — "who is waiting on me", "who have I answered" —
# have nothing to show. `_state` is the state each one is seeded into.
DEMO_LIFECYCLE = [
    {
        "id": "lead-1004", "traveler": "Sam (demo)", "received": "3 days ago",
        "title": "6-month stay near the hospital", "move_in": "Oct 1",
        "move_out": "Mar 31", "nights": 182, "occupants": 1,
        "detail": "Residency placement, needs a quiet desk.",
        "_state": "awaiting_guest", "_unit": "unit-2",
    },
    {
        "id": "lead-1005", "traveler": "Alex (demo)", "received": "6 days ago",
        "title": "Studio for a winter rotation", "move_in": "Dec 1",
        "move_out": "Feb 28", "nights": 90, "occupants": 1,
        "detail": "Winter contract, flexible on exact dates.",
        "_state": "scheduled", "_unit": "unit-2",
    },
    {
        "id": "lead-1006", "traveler": "Robin (demo)", "received": "9 days ago",
        "title": "Relocating in the spring", "move_in": "Mar 1",
        "move_out": "Aug 31", "nights": 183, "occupants": 2,
        "detail": "Confirmed and signed — arriving in the spring.",
        "_state": "booked", "_unit": "unit-1",
    },
    {
        "id": "lead-1007", "traveler": "Casey (demo)", "received": "12 days ago",
        "title": "Short notice stay", "move_in": "Aug 25", "nights": 45,
        "occupants": 1, "detail": "Went quiet after two follow-ups.",
        "_state": "lost", "_unit": "unit-1",
    },
]


def _reset_sample_data(tenant_id: str) -> None:
    """Clear this tenant's seen/response/deal rows so re-seeding is idempotent.

    Uses storage._conn so the tables are created/migrated if they don't exist yet.
    """
    import pipeline

    with storage._conn() as c:
        c.execute("DELETE FROM seen WHERE tenant_id=? AND site=?", (tenant_id, SITE))
        c.execute("DELETE FROM responses WHERE tenant_id=? AND site=?", (tenant_id, SITE))
    # Deals outlive a scrape, so clearing only seen/responses left the previous
    # run's deals behind and re-seeding accumulated duplicates.
    with pipeline._conn() as c:
        c.execute("DELETE FROM deals WHERE tenant_id=? AND site=?", (tenant_id, SITE))


def _seed_lifecycle(tenant_id: str) -> None:
    """Open a deal per lifecycle sample and move it into its intended state."""
    import pipeline

    items = [{k: v for k, v in d.items() if not k.startswith("_")}
             for d in DEMO_LIFECYCLE]
    storage.filter_new(tenant_id, SITE, "lead", items)
    for sample, item in zip(DEMO_LIFECYCLE, items):
        unit = sample["_unit"]
        storage.save_response(
            tenant_id, SITE, "lead", item["id"], status="sent", unit_id=unit,
            confidence="high", reason="Seeded demo conversation.",
            draft="Thanks for reaching out — happy to help with dates and details.",
        )
        pipeline.ensure(tenant_id, SITE, item,
                        storage.get_responses(tenant_id, SITE).get(item["id"]),
                        units=DEMO_UNITS)
        pipeline.record_contact(tenant_id, SITE, item["id"])

        state = sample["_state"]
        if state == "scheduled":
            pipeline.update(tenant_id, SITE, item["id"], stage=pipeline.NURTURING,
                            next_action_at="2026-12-01T09:00:00",
                            next_action_step="presale_followup_1")
        elif state == "booked":
            pipeline.mark_booked(tenant_id, SITE, item["id"])
        elif state == "lost":
            pipeline.mark_lost(tenant_id, SITE, item["id"])


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
    _seed_lifecycle(tenant_id)
    return DEMO_EMAIL, tenant_id


if __name__ == "__main__":
    email, tid = seed_demo()
    print(f"Demo tenant ready: {email} (tenant {tid})")
    print(f"  Password: {DEMO_PASSWORD}")
    print("  Log in and you'll see sample leads/messages with drafts waiting.")
