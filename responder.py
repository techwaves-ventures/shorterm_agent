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
from pathlib import Path

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _find_email(item: dict) -> str | None:
    """Best-effort email scrape from the raw inquiry text (model fallback)."""
    for key in ("body", "raw", "title"):
        text = item.get(key)
        if text:
            m = _EMAIL_RE.search(text)
            if m:
                return m.group(0)
    return None

MODEL = "claude-sonnet-4-6"
_BASE = Path(__file__).parent
UNITS_PATH = _BASE / "units.json"
TEMPLATE_PATH = _BASE / "response_template.md"

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

Decide fit in this priority order:
1. LOCATION / AREA — if the tenant clearly wants a different city or area than any \
unit serves, it is NOT a fit (fit=false). This is the most common skip.
2. BUDGET / PRICE — if the tenant states a budget and it's below a unit's \
monthly_price (when monthly_price > 0), that unit doesn't fit.
3. OCCUPANCY / PETS / LEASE LENGTH — guests must be <= max_occupancy (when set > 0); \
pets only if pets_allowed; requested stay should fall within min_nights..max_nights \
when the tenant states a duration.

Pick the SINGLE best-fitting unit. If no unit fits, set fit=false, unit_id=null, \
draft=null, and give a short reason. If a unit fits, write the draft per the style \
guide below. Treat fields that are 0 / unset as "unknown" — don't skip a lead just \
because a unit's number is missing; only skip on a clear mismatch.

Only state unit facts that appear in the unit catalog. Never invent amenities, \
prices, or availability.

If the inquiry text contains the tenant's email address, return it in tenant_email; \
otherwise return null. Never guess an email.

You must call the record_decision tool with your decision.

=== UNIT CATALOG ===
{units}

=== RESPONSE STYLE GUIDE ===
{guide}
"""


def load_units() -> list[dict]:
    try:
        return json.loads(UNITS_PATH.read_text())
    except (OSError, ValueError):
        log.warning("Could not read %s", UNITS_PATH)
        return []


def _load_guide() -> str:
    try:
        return TEMPLATE_PATH.read_text()
    except OSError:
        return ""


def _system_blocks(units: list[dict]) -> list[dict]:
    """Stable system prompt (units + style guide), cached as a prefix."""
    text = SYSTEM_GUIDE.format(
        units=json.dumps(units, indent=2),
        guide=_load_guide(),
    )
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _lead_text(item: dict) -> str:
    """The only per-lead (uncached) input — placed after the cached prefix."""
    fields = {
        k: item.get(k)
        for k in ("kind", "traveler", "sender", "received", "date", "title", "raw", "body")
        if item.get(k)
    }
    return (
        "Here is one tenant inquiry. Decide fit, pick the best unit, and draft a reply.\n\n"
        + json.dumps(fields, indent=2)
    )


def evaluate_lead(item: dict, units: list[dict] | None = None, client=None) -> dict:
    """Return {fit, unit_id, reason, draft, confidence}. Raises on API/config error."""
    import anthropic

    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env — cannot draft replies.")

    units = units if units is not None else load_units()
    client = client or anthropic.Anthropic()

    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=_system_blocks(units),
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
