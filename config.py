"""Per-tenant configuration — units, response template, sign-off, sending.

Each tenant gets one `tenant_settings` row (in the shared leads.db) holding what
used to be global: units.json, response_template.md, HOST_NAME, REPLY_CHANNELS,
FROM_EMAIL. The responder/runner/mailer read these per tenant so two hosts get
their own units, voice, and identity.

SMTP host/user/pass remain a shared global relay (mailer.py); only the From
address is per tenant in this phase.
"""
import json
import os
from datetime import datetime
from pathlib import Path

import db

_BASE = Path(__file__).parent

# Embedded so brand-new tenants get a working starter without reading a file.
# Kept in sync with response_template.md (the operator's seed source).
DEFAULT_TEMPLATE = """# Response style guide

Edit this to change the voice of auto-drafted replies. The agent personalizes
around this guidance — it does not fill in blanks literally.

## Tone
- Warm, professional, and concise. Sound like a real host, not a form letter.
- Write in first person ("I", "my place"). 80–140 words.

## Introductions — for NEW LEADS (they haven't messaged you)
The tenant only expressed interest in the listing; this is your first contact.
1. Greet the tenant by first name.
2. Introduce yourself/the place warmly and say you'd love to host them.
3. Name 1–2 concrete details from the unit's facts (only catalog facts — never
   invent amenities, prices, or availability).
4. Invite them to share their dates, who's traveling, and any needs — and offer
   a call or tour.
5. Sign off with the host's first name — never a generic placeholder like
   "Your host" or "[Your name]".
- Do NOT thank them for a message: they haven't sent one.

## Replies — for MESSAGES (they wrote to you)
The tenant sent you a message; respond to it.
1. Greet the tenant by first name.
2. Acknowledge their specific message — reference something concrete they said
   and answer any question they asked.
3. Confirm the unit is a good fit and name 1–2 concrete catalog details that
   match what they asked about.
4. Invite a next step (a call, a tour, or to confirm dates).
5. Sign off with the host's first name.

## Rules
- Only state unit facts that appear in the unit catalog. If a detail isn't
  listed, don't mention it — ask or stay general instead.
- Don't quote a price unless monthly_price is set (> 0) in the catalog.
- No emojis. No "Dear Sir/Madam". No pressure tactics.
"""

# Editable per-tenant fields. Order defines the SELECT/INSERT column order below.
# `onboarded` ('0'/'1') gates the first-run wizard; `notify_webhook` is a
# per-tenant Slack/Discord-style URL (falls back to the global NOTIFY_WEBHOOK_URL).
_FIELDS = (
    "host_name", "units_json", "template", "from_email", "reply_channels",
    "notify_webhook", "onboarded",
)

# Columns added after the original schema — created idempotently on older DBs.
_ADDED_COLUMNS = {"notify_webhook": "TEXT", "onboarded": "TEXT DEFAULT '0'"}


def _conn() -> db.Conn:
    c = db.connect()
    c.execute(
        """CREATE TABLE IF NOT EXISTS tenant_settings (
            tenant_id TEXT PRIMARY KEY,
            host_name TEXT,
            units_json TEXT,
            template TEXT,
            from_email TEXT,
            reply_channels TEXT,
            notify_webhook TEXT,
            onboarded TEXT DEFAULT '0',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    # Idempotent migration for DBs created before these columns existed.
    have = db.table_columns(c, "tenant_settings")
    for col, decl in _ADDED_COLUMNS.items():
        if col not in have:
            c.execute(f"ALTER TABLE tenant_settings ADD COLUMN {col} {decl}")
    return c


def _defaults(tenant_id: str) -> dict:
    return {
        "tenant_id": tenant_id,
        "host_name": "",
        "units_json": "[]",
        "template": DEFAULT_TEMPLATE,
        "from_email": "",
        "reply_channels": "platform,email",
        "notify_webhook": "",
        "onboarded": "0",
    }


def _read_row(c, tenant_id: str):
    return c.execute(
        f"SELECT {', '.join(_FIELDS)} FROM tenant_settings WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()


def _row_to_dict(tenant_id: str, row) -> dict:
    """Overlay a DB row onto the defaults, keeping the default for NULL columns."""
    out = _defaults(tenant_id)
    for key, val in zip(_FIELDS, row):
        if val is not None:
            out[key] = val
    if not out["units_json"]:
        out["units_json"] = "[]"
    if not out["reply_channels"]:
        out["reply_channels"] = "platform,email"
    return out


def get_settings(tenant_id: str) -> dict:
    """Return this tenant's settings as a dict, seeding defaults if absent."""
    with _conn() as c:
        row = _read_row(c, tenant_id)
    if row is None:
        seed_tenant(tenant_id)
        with _conn() as c:
            row = _read_row(c, tenant_id)
    return _row_to_dict(tenant_id, row)


def save_settings(tenant_id: str, **fields) -> None:
    """Upsert the given setting fields for a tenant. Stamps updated_at.

    Reads the existing row directly (not via get_settings) so it can be called
    from seed_tenant without recursing through the auto-seed path.
    """
    sets = {k: v for k, v in fields.items() if k in _FIELDS}
    if not sets:
        return
    with _conn() as c:
        row = _read_row(c, tenant_id)
    current = _row_to_dict(tenant_id, row) if row is not None else _defaults(tenant_id)
    current.update(sets)
    now = datetime.now().isoformat(timespec="seconds")
    cols = list(_FIELDS)
    placeholders = ",".join(["?"] * (len(cols) + 2))  # tenant_id + fields + updated_at
    assignments = ", ".join(f"{c2}=excluded.{c2}" for c2 in cols + ["updated_at"])
    vals = [tenant_id] + [current[k] for k in cols] + [now]
    with _conn() as c:
        c.execute(
            f"""INSERT INTO tenant_settings (tenant_id, {", ".join(cols)}, updated_at)
                VALUES ({placeholders})
                ON CONFLICT(tenant_id) DO UPDATE SET {assignments}""",
            vals,
        )


def is_onboarded(tenant_id: str) -> bool:
    """Whether this tenant has completed the first-run onboarding wizard."""
    return str(get_settings(tenant_id).get("onboarded")) in ("1", "true", "True")


def mark_onboarded(tenant_id: str) -> None:
    save_settings(tenant_id, onboarded="1")


def get_units(tenant_id: str) -> list[dict]:
    """Parse this tenant's units array. Returns [] on missing/invalid JSON."""
    raw = get_settings(tenant_id)["units_json"]
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


def validate_units(text: str) -> list[dict]:
    """Parse + validate a units JSON string for the Settings form.

    Returns the parsed list. Raises ValueError with a readable message that the
    form surfaces to the user.
    """
    text = (text or "").strip()
    if not text:
        return []
    try:
        val = json.loads(text)
    except ValueError as e:
        raise ValueError(f"Invalid JSON: {e}")
    if not isinstance(val, list):
        raise ValueError("Units must be a JSON array (a list of unit objects).")
    for i, u in enumerate(val):
        if not isinstance(u, dict):
            raise ValueError(f"Unit #{i + 1} must be an object with fields like name, area, etc.")
    return val


def seed_tenant(tenant_id: str, *, from_legacy: bool = False) -> None:
    """Create a settings row for a tenant if one doesn't exist.

    `from_legacy=True` (the operator) seeds from the old global units.json /
    response_template.md / env so existing behavior is preserved. Otherwise a
    fresh tenant gets empty units + the default template. No-op if a row already
    exists, so it never clobbers later edits.
    """
    with _conn() as c:
        exists = c.execute(
            "SELECT 1 FROM tenant_settings WHERE tenant_id=?", (tenant_id,)
        ).fetchone()
    if exists:
        return

    vals = _defaults(tenant_id)
    if from_legacy:
        # The operator predates onboarding — grandfather them past the wizard.
        vals["onboarded"] = "1"
        try:
            vals["units_json"] = (_BASE / "units.json").read_text().strip() or "[]"
        except OSError:
            pass
        try:
            vals["template"] = (_BASE / "response_template.md").read_text()
        except OSError:
            pass
        vals["host_name"] = (os.getenv("HOST_NAME") or "").strip()
        vals["from_email"] = (os.getenv("FROM_EMAIL") or "").strip()
        vals["reply_channels"] = (
            os.getenv("REPLY_CHANNELS") or "platform,email"
        ).strip()

    save_settings(tenant_id, **{k: vals[k] for k in _FIELDS})
