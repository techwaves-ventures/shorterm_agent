"""Lead-response agent.

Given a FurnishedFinder lead/message and the unit catalog, Claude decides:
  - is this a good fit? (location > budget > occupancy/pets/lease-length)
  - which single unit fits best?
  - a warm, personalized draft reply (or skip with a reason).

Nothing is sent here — `evaluate_lead` only returns a decision. Sending is the
dashboard's one-click job (see runner.send_reply).
"""
import json
import logging
import os
import re

import config

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _find_email(item: dict) -> str | None:
    """Best-effort email scrape from the raw inquiry text (model fallback)."""
    # The lead detail view often carries the address even when the row doesn't,
    # so a parsed `email` and the full `detail` text are checked first.
    if item.get("email"):
        return item["email"]
    for key in ("detail", "body", "raw", "title"):
        text = item.get(key)
        if text:
            m = _EMAIL_RE.search(text)
            if m:
                return m.group(0)
    return None

MODEL = "claude-sonnet-4-6"

# Forced-tool schema. strict=True guarantees a valid, parseable decision.
DECISION_TOOL = {
    "name": "record_decision",
    "description": "Record the fit decision and draft reply for this lead.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "fit": {
                "type": "boolean",
                "description": "True if one of our units is a good fit for this lead.",
            },
            "unit_id": {
                "type": ["string", "null"],
                "description": "The id of the single best-fitting unit, or null if no fit.",
            },
            "reason": {
                "type": "string",
                "description": "One short sentence explaining the fit/skip decision.",
            },
            "draft": {
                "type": ["string", "null"],
                "description": "The personalized reply to send, or null if skipping.",
            },
            "confidence": {
                "type": "string",
                "enum": ["low", "med", "high"],
                "description": "Confidence in the fit decision.",
            },
            "tenant_email": {
                "type": ["string", "null"],
                "description": "The tenant's email address if it appears in the inquiry, else null.",
            },
        },
        "required": ["fit", "unit_id", "reason", "draft", "confidence", "tenant_email"],
        "additionalProperties": False,
    },
}

SYSTEM_GUIDE = """You are the outreach assistant for a short-term-rental host on \
FurnishedFinder. For each tenant inquiry you decide whether one of the host's units \
is a good fit, pick the single best-fitting unit, and write a personalized reply.

YOUR DEFAULT IS TO REPLY. Every inquiry here was sent BY a tenant ABOUT one of this \
host's specific listings (see the "Property:" field) — they already chose the place. \
The host's business depends on answering them. Skipping is a rare exception, not a \
screening step: a missed good tenant costs the host a booking, while a reply to an \
imperfect one costs nothing but a message.

CRITICAL — do NOT skip on location. The "Location:" field in an inquiry is where the \
tenant CURRENTLY LIVES (the city they are moving FROM). It is NOT where they want to \
rent. A tenant in another state, city, or suburb is completely normal and is usually \
a RELOCATION — exactly the business this host wants. The desired location is already \
settled: it is the property they inquired about. Never set fit=false because the \
tenant's stated location differs from the unit's area.

Set fit=false ONLY when there is a HARD, EXPLICIT, UNRESOLVABLE mismatch:
- The tenant states a maximum budget clearly BELOW the unit's monthly_price \
(only when monthly_price is set and > 0). "No Max" or a blank budget is never a skip.
- The number of travelers clearly EXCEEDS max_occupancy (only when set > 0).
- The tenant is travelling with pets AND pets_allowed is explicitly false for every unit.
- The requested stay is clearly outside min_nights..max_nights.
- The message is not from a prospective tenant at all (spam, a vendor, another host).

Anything unknown, unset, 0, blank, "-", or ambiguous is NOT a mismatch — it is a \
question to ask in the reply. If you are unsure, REPLY. When only one detail is a \
problem but the tenant looks otherwise strong, still reply and raise that detail \
honestly rather than skipping.

Pick the SINGLE best-fitting unit — normally the one named in the "Property:" field \
(match it against each unit's listing_match/name). If a unit fits, write the draft per \
the style guide below. If you genuinely must skip, set fit=false, unit_id=null, \
draft=null, and give a short, specific reason naming the hard mismatch.

Only state unit facts that appear in the unit catalog. Never invent amenities, \
prices, or availability.

If the inquiry text contains the tenant's email address, return it in tenant_email; \
otherwise return null. Never guess an email.

When a unit fits, write the draft in the FIRST PERSON as the host, {host_name}. \
Always sign off the reply with the host's name, "{host_name}" — never a generic \
placeholder like "Your host", "The host", or "[Your name]".

You must call the record_decision tool with your decision.

=== UNIT CATALOG ===
{units}

=== RESPONSE STYLE GUIDE ===
{guide}
"""


def load_units(tenant_id: str) -> list[dict]:
    """This tenant's unit catalog (from per-tenant config)."""
    return config.get_units(tenant_id)


def _system_blocks(units: list[dict], guide: str, host_name: str) -> list[dict]:
    """Stable system prompt (units + style guide), cached as a prefix.

    The prefix is per-tenant now (units/guide/host differ), so caching is
    effective across a tenant's repeated drafts rather than across tenants.
    """
    text = SYSTEM_GUIDE.format(
        host_name=host_name or "your host",
        units=json.dumps(units, indent=2),
        guide=guide or "",
    )
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


_LEAD_MODE = (
    "MODE: INTRODUCTION (new lead).\n"
    "This tenant has NOT messaged you yet — they only expressed interest in your "
    "listing. Write a warm INTRODUCTION that opens the conversation: greet them, "
    "introduce the place, and invite them to share their dates and details. "
    "Do NOT thank them for a message — they haven't sent one. Avoid phrases like "
    "\"thanks for reaching out\" or \"thanks for your message\"."
)

_MESSAGE_MODE = (
    "MODE: REPLY (the tenant messaged you).\n"
    "This tenant sent you the message shown below. Write a reply that responds "
    "directly to what they actually said — answer their questions and reference "
    "the specifics of their message."
)


def _lead_text(item: dict) -> str:
    """The only per-item (uncached) input — placed after the cached prefix.

    Carries the reply MODE so leads get an introduction and messages get a
    response. Kept out of the cached system block so caching stays effective.
    """
    mode = _MESSAGE_MODE if item.get("kind") == "message" else _LEAD_MODE
    fields = {
        k: item.get(k)
        for k in (
            "kind", "traveler", "sender", "received", "date", "title",
            # Lead detail-view facts (present when the detail scrape succeeds).
            "move_in", "move_out", "nights", "occupants", "pets", "budget", "phone",
            "raw", "body", "detail",
        )
        if item.get(k) is not None and item.get(k) != ""
    }
    return (
        mode
        + "\n\nDecide fit, pick the best unit, and draft the reply for this "
        "tenant inquiry:\n\n"
        + json.dumps(fields, indent=2)
    )


# Forced-tool schema for a lifecycle step (follow-up, pre-arrival, etc.). Fit was
# already decided when the deal was created, so a step only produces the message
# — plus an explicit `skip` escape hatch for when sending would be wrong (e.g.
# the guest already answered the question the follow-up would ask).
STEP_TOOL = {
    "name": "record_message",
    "description": "Record the message to send for this lifecycle step.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "message": {
                "type": ["string", "null"],
                "description": "The message to send, or null when skipping.",
            },
            "skip": {
                "type": "boolean",
                "description": "True if this step should NOT be sent for this guest.",
            },
            "reason": {
                "type": "string",
                "description": "One short sentence explaining the message or the skip.",
            },
        },
        "required": ["message", "skip", "reason"],
        "additionalProperties": False,
    },
}

STEP_SYSTEM = """You are the messaging assistant for a short-term-rental host, \
writing as the host, {host_name}. You handle the whole guest lifecycle: the first \
reply to an inquiry, follow-ups when a guest goes quiet, and the logistics messages \
after they book (confirmation, pre-arrival, check-in, welcome, checkout).

Absolute rules:
- Only state facts that appear in the unit catalog or in the guest's own messages. \
NEVER invent amenities, prices, addresses, access codes, policies, or availability.
- If a needed detail is missing, say so plainly or ask — never fabricate a plausible \
substitute. A wrong access code or address causes real harm.
- Write in the first person as {host_name} and sign off with that name.
- Match the style guide below. No emojis. No pressure tactics.
- Set skip=true if this message would be inappropriate, redundant, or if the guest \
has already moved past this stage.

=== UNIT CATALOG ===
{units}

=== RESPONSE STYLE GUIDE ===
{guide}
"""


def draft_step(item: dict, tenant_id: str, deal: dict, step: dict,
               units: list[dict] | None = None, history: list[str] | None = None,
               client=None) -> dict:
    """Draft one lifecycle step for a deal.

    Returns {message, skip, reason}. `step` is a sequences.py definition whose
    `guidance` describes the intent of this particular touch; `history` is the
    text we have already sent, so the model doesn't repeat itself.
    """
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env — cannot draft replies.")

    settings = config.get_settings(tenant_id)
    units = units if units is not None else config.get_units(tenant_id)
    if client is None:
        base_url = os.getenv("ANTHROPIC_RESPONDER_BASE_URL", "https://api.anthropic.com")
        client = anthropic.Anthropic(api_key=api_key, base_url=base_url)

    system = STEP_SYSTEM.format(
        host_name=settings["host_name"] or "your host",
        units=json.dumps(units, indent=2),
        guide=settings["template"] or "",
    )

    context = {
        "guest": deal.get("guest_name"),
        "stage": deal.get("stage"),
        "unit_id": deal.get("unit_id"),
        "check_in": deal.get("check_in"),
        "check_out": deal.get("check_out"),
        "nights": deal.get("nights"),
    }
    inquiry = {
        k: item.get(k)
        for k in ("traveler", "sender", "received", "date", "title", "move_in",
                  "move_out", "nights", "occupants", "pets", "budget", "raw",
                  "body", "detail")
        if item.get(k)
    }
    parts = [
        f"STEP: {step['label']}\n{step['guidance']}",
        "\nDEAL CONTEXT:\n" + json.dumps(context, indent=2),
        "\nORIGINAL INQUIRY:\n" + json.dumps(inquiry, indent=2),
    ]
    if history:
        parts.append(
            "\nALREADY SENT TO THIS GUEST (do not repeat):\n"
            + "\n---\n".join(history[-3:])
        )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        tools=[STEP_TOOL],
        tool_choice={"type": "tool", "name": "record_message"},
        messages=[{"role": "user", "content": "\n".join(parts)}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "record_message":
            d = dict(block.input)
            log.info(
                "Step %s for deal %s: skip=%s", step["id"], item.get("id"), d.get("skip")
            )
            return d
    raise RuntimeError("Model did not return a record_message tool call.")


def evaluate_lead(item: dict, tenant_id: str, units: list[dict] | None = None,
                  client=None) -> dict:
    """Return {fit, unit_id, reason, draft, confidence}. Raises on API/config error.

    `tenant_id` selects which host's units, template, and sign-off name to use.
    """
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env — cannot draft replies.")

    settings = config.get_settings(tenant_id)
    units = units if units is not None else config.get_units(tenant_id)
    if client is None:
        # Talk to Anthropic directly. Pass api_key + base_url explicitly so a
        # host-shell ANTHROPIC_BASE_URL (e.g. a Claude Code proxy) can't
        # redirect our key to the wrong endpoint. Override with
        # ANTHROPIC_RESPONDER_BASE_URL only if you intend to use a proxy.
        base_url = os.getenv("ANTHROPIC_RESPONDER_BASE_URL", "https://api.anthropic.com")
        client = anthropic.Anthropic(api_key=api_key, base_url=base_url)

    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=_system_blocks(units, settings["template"], settings["host_name"]),
        tools=[DECISION_TOOL],
        tool_choice={"type": "tool", "name": "record_decision"},
        messages=[{"role": "user", "content": _lead_text(item)}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "record_decision":
            decision = dict(block.input)
            # Regex fallback when the model didn't surface an email.
            if not decision.get("tenant_email"):
                decision["tenant_email"] = _find_email(item)
            log.info(
                "Decision for %s: fit=%s unit=%s email=%s (cache_read=%s)",
                item.get("id"),
                decision.get("fit"),
                decision.get("unit_id"),
                bool(decision.get("tenant_email")),
                resp.usage.cache_read_input_tokens,
            )
            return decision
    raise RuntimeError("Model did not return a record_decision tool call.")
