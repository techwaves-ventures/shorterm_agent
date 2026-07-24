"""Agentic messaging playbooks for the two halves of the guest lifecycle.

**Pre-sale** (inquiry -> booking): the intro reply plus the follow-ups owners
reliably fail to send. Most lost deals die of silence, not of a bad first
message, so this is where the agent earns its keep.

**Pre-arrival** (booking -> arrival -> stay): the logistics stream — confirmation,
the week-before note, check-in details, welcome, mid-stay, checkout. This half
is largely operational, so it is both safer to automate and the part owners most
want off their plate.

Each step declares *when* it fires (an anchor + offset) and *what* the agent
should write (`guidance`, injected into the responder's prompt). Nothing here
talks to a model or a browser; automation.py executes these definitions.

Safety model — three layers, deliberately conservative:
  1. Automation is off per tenant until explicitly enabled; until then every
     step just queues a draft for approval (the app's original posture).
  2. `auto_send_default` is False for the whole pre-sale sequence — a weak first
     impression to a live prospect is expensive and hard to undo.
  3. `never_auto` steps can never send unattended regardless of settings,
     because they carry access credentials (see CHECK_IN_DETAILS).
"""
from datetime import datetime, time, timedelta

# Sends are clamped into these local hours: an automated 3am message reads as a
# bot and burns the trust the product is selling.
QUIET_START = time(8, 0)
QUIET_END = time(20, 0)

PRESALE = "presale"
PREARRIVAL = "prearrival"

# Anchors a step's timing can hang off. 'last_contact' makes follow-ups relative
# to our most recent message, so a manual reply correctly resets the clock.
A_INQUIRY = "inquiry_at"
A_LAST_CONTACT = "last_contact_at"
A_CHECK_IN = "check_in"
A_CHECK_OUT = "check_out"


SEQUENCES: dict[str, dict] = {
    PRESALE: {
        "id": PRESALE,
        "label": "Pre-booking nurture",
        "blurb": "Reply fast, then follow up until they book or clearly pass.",
        "steps": [
            {
                "id": "intro",
                "label": "First reply",
                "anchor": A_INQUIRY,
                "offset_hours": 0,
                "auto_send_default": False,
                "guidance": (
                    "This is the FIRST contact. Introduce the place warmly, ground it in "
                    "1-2 concrete catalog facts, and invite them to share dates and details. "
                    "Do not thank them for a message — they have not sent one."
                ),
            },
            {
                "id": "followup_1",
                "label": "Follow-up #1",
                "anchor": A_LAST_CONTACT,
                "offset_hours": 48,
                "auto_send_default": False,
                "guidance": (
                    "They have not replied in ~2 days. Send a SHORT, warm nudge (under 60 "
                    "words). Add one genuinely new concrete detail from the catalog, or ask "
                    "one specific easy-to-answer question about their dates or group. "
                    "Do not repeat the original message and do not sound automated."
                ),
            },
            {
                "id": "followup_2",
                "label": "Follow-up #2",
                "anchor": A_LAST_CONTACT,
                "offset_hours": 120,
                "auto_send_default": False,
                "guidance": (
                    "Still no reply after ~5 days. Offer a concrete next step: a quick call, "
                    "a video tour, or flexibility on the move-in date. Ask whether their "
                    "plans have changed. Keep it under 60 words and low-pressure."
                ),
            },
            {
                "id": "last_call",
                "label": "Final check-in",
                "anchor": A_LAST_CONTACT,
                "offset_hours": 240,
                "auto_send_default": False,
                "guidance": (
                    "Final touch after ~10 days of silence. Politely close the loop: say you "
                    "will stop following up, leave the door genuinely open if their plans "
                    "change, and wish them well. No pressure, no guilt, under 50 words."
                ),
            },
        ],
    },
    PREARRIVAL: {
        "id": PREARRIVAL,
        "label": "Booked → arrival",
        "blurb": "Confirmation, the week-before note, check-in details, welcome and checkout.",
        "steps": [
            {
                "id": "booking_confirmed",
                "label": "Booking confirmation",
                "anchor": A_LAST_CONTACT,
                "offset_hours": 0,
                "auto_send_default": True,
                "guidance": (
                    "They have just booked. Confirm warmly and restate ONLY the facts you "
                    "were given: the unit, the confirmed dates. Tell them you will send "
                    "arrival details closer to the date. Do not invent prices, deposits, "
                    "contracts, or policies."
                ),
            },
            {
                "id": "pre_arrival_week",
                "label": "One week before",
                "anchor": A_CHECK_IN,
                "offset_hours": -168,
                "auto_send_default": True,
                "guidance": (
                    "Arrival is about a week away. Practical and friendly: confirm the "
                    "check-in date, ask for their approximate arrival time and how they are "
                    "travelling in, and invite any questions. Mention only amenities that "
                    "appear in the unit catalog."
                ),
            },
            {
                "id": "check_in_details",
                "label": "Check-in instructions",
                "anchor": A_CHECK_IN,
                "offset_hours": -24,
                "auto_send_default": False,
                # Access credentials must never be sent by an unattended agent: a
                # hallucinated door code locks a real guest out of a real house,
                # and a leaked correct one is a security incident. Always human-approved.
                "never_auto": True,
                "guidance": (
                    "Arrival is tomorrow. Give the practical arrival rundown using ONLY "
                    "details present in the unit catalog notes — address, access/parking, "
                    "arrival window, contact number. CRITICAL: if an access code, key "
                    "location, or exact address is not in the catalog, do NOT invent one. "
                    "Write [ADD ACCESS DETAILS] in its place so the host fills it in."
                ),
            },
            {
                "id": "welcome",
                "label": "Arrival day welcome",
                "anchor": A_CHECK_IN,
                "offset_hours": 4,
                "auto_send_default": True,
                "guidance": (
                    "They arrive today. Short, warm welcome. Invite them to message if "
                    "anything is unclear or not as expected. Under 50 words."
                ),
            },
            {
                "id": "mid_stay",
                "label": "Mid-stay check-in",
                "anchor": A_CHECK_IN,
                "offset_hours": 336,
                "auto_send_default": True,
                "guidance": (
                    "They have been in the place about two weeks. Brief, genuine check-in: "
                    "is everything working, anything they need. This is a service touch, "
                    "not a sales message. Under 50 words."
                ),
            },
            {
                "id": "checkout_info",
                "label": "Checkout details",
                "anchor": A_CHECK_OUT,
                "offset_hours": -48,
                "auto_send_default": True,
                "guidance": (
                    "Checkout is in two days. Confirm the checkout date, what to do with "
                    "keys, and any wrap-up steps that appear in the catalog notes. Thank "
                    "them warmly. If a checkout procedure is not documented, ask them to "
                    "confirm their departure time instead of inventing instructions."
                ),
            },
        ],
    },
}


def get(sequence_id: str | None) -> dict | None:
    return SEQUENCES.get(sequence_id or "")


def steps(sequence_id: str | None) -> list[dict]:
    seq = get(sequence_id)
    return seq["steps"] if seq else []


def step_at(sequence_id: str | None, index: int) -> dict | None:
    all_steps = steps(sequence_id)
    if 0 <= index < len(all_steps):
        return all_steps[index]
    return None


def find_step(sequence_id: str | None, step_id: str) -> dict | None:
    for s in steps(sequence_id):
        if s["id"] == step_id:
            return s
    return None


def can_auto_send(step: dict, enabled_steps: set[str]) -> bool:
    """Whether this step may send unattended.

    `never_auto` wins over any configuration — that flag exists precisely so a
    settings toggle can't accidentally arm a credential-bearing message.
    """
    if step.get("never_auto"):
        return False
    return step["id"] in enabled_steps


def default_enabled_steps() -> set[str]:
    """Steps that are auto-send-eligible out of the box (pre-arrival logistics)."""
    return {
        s["id"]
        for seq in SEQUENCES.values()
        for s in seq["steps"]
        if s.get("auto_send_default") and not s.get("never_auto")
    }


def _anchor_dt(deal: dict, anchor: str) -> datetime | None:
    """Resolve a step's anchor to a datetime on the deal.

    Date-only anchors (check_in / check_out) are pinned to 09:00 so an offset of
    '-24h' lands the evening before rather than at midnight.
    """
    raw = deal.get(anchor)
    if not raw:
        # A follow-up on a deal we have never contacted still needs a clock;
        # fall back to when the guest first asked.
        if anchor == A_LAST_CONTACT:
            raw = deal.get(A_INQUIRY)
        if not raw:
            return None
    try:
        if len(str(raw)) == 10:  # YYYY-MM-DD
            return datetime.combine(datetime.fromisoformat(str(raw)).date(), time(9, 0))
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _clamp_quiet_hours(dt: datetime) -> datetime:
    """Nudge a send time into waking hours (see QUIET_START/QUIET_END)."""
    if dt.time() < QUIET_START:
        return datetime.combine(dt.date(), QUIET_START)
    if dt.time() > QUIET_END:
        return datetime.combine(dt.date() + timedelta(days=1), QUIET_START)
    return dt


def due_at(deal: dict, step: dict) -> str | None:
    """When `step` should fire for `deal`, or None if its anchor is unknown.

    Returning None is meaningful: a pre-arrival step can't be scheduled until we
    know the check-in date, so the deal simply has no next action until the
    owner supplies one.
    """
    anchor = _anchor_dt(deal, step["anchor"])
    if anchor is None:
        return None
    return _clamp_quiet_hours(
        anchor + timedelta(hours=step.get("offset_hours", 0))
    ).isoformat(timespec="seconds")


def schedule(deal: dict) -> tuple[str | None, str | None]:
    """(next_action_at, step_id) for the deal's current position in its sequence.

    Returns (None, None) when the sequence is finished or unschedulable.
    """
    step = step_at(deal.get("sequence"), int(deal.get("step_index") or 0))
    if not step:
        return None, None
    return due_at(deal, step), step["id"]


def is_last_step(sequence_id: str | None, index: int) -> bool:
    return index >= len(steps(sequence_id)) - 1
