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
import re
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
    "notify_webhook", "onboarded", "automation_enabled", "auto_steps",
    "autopilot", "check_times", "last_check_at",
    "timezone", "digest_enabled", "digest_hour", "last_digest_at",
    "ingest_mode",
)

# Columns added after the original schema — created idempotently on older DBs.
_ADDED_COLUMNS = {
    "notify_webhook": "TEXT",
    "onboarded": "TEXT DEFAULT '0'",
    # Master switch for unattended sending. '0' (off) keeps the original
    # human-approves-everything posture; the agent still drafts and schedules.
    "automation_enabled": "TEXT DEFAULT '0'",
    # Comma-separated sequence step ids allowed to send without approval.
    # NULL means "use the safe defaults" (see sequences.default_enabled_steps).
    "auto_steps": "TEXT",
    # Autopilot: check FurnishedFinder on a schedule and reply to good fits
    # without waiting for a human. Off until the owner turns it on.
    "autopilot": "TEXT DEFAULT '0'",
    # Local times of day to run an automatic check, e.g. "09:00,16:00".
    "check_times": "TEXT",
    # When the last automatic check fired (so a slot only fires once).
    "last_check_at": "TEXT",
    # IANA zone for this host's properties ("America/New_York"). Schedules are
    # local to the property, not to wherever the server happens to run.
    "timezone": "TEXT",
    # End-of-day summary email.
    "digest_enabled": "TEXT DEFAULT '1'",
    "digest_hour": "TEXT",
    "last_digest_at": "TEXT",
    # How leads are read: 'email' (forwarded FurnishedFinder notifications, no
    # browser involved) or 'browser' (scheduled scraping). Email is the default
    # for new tenants because it keeps automation off FurnishedFinder entirely.
    "ingest_mode": "TEXT DEFAULT 'email'",
}

# Default automatic check schedule: twice a day inside business hours.
DEFAULT_CHECK_TIMES = "09:00,16:00"
# Property-local timezone used for every schedule. Blank means "the server's
# zone", which is only right for a single-market operator — hosted deploys run
# in UTC, so each tenant sets their own.
DEFAULT_TIMEZONE = ""
# End-of-day digest, in the property's local time.
DEFAULT_DIGEST_HOUR = "18:00"

# Lead ingestion modes.
INGEST_EMAIL = "email"      # forwarded notifications; never reads their site
INGEST_BROWSER = "browser"  # scheduled scraping (legacy / fallback)


def ingest_mode(tenant_id: str) -> str:
    """How this tenant's leads are read. Anything unrecognised means email —
    the safe default, since it involves no automated access to FurnishedFinder."""
    value = str(get_settings(tenant_id).get("ingest_mode") or "").strip().lower()
    return INGEST_BROWSER if value == INGEST_BROWSER else INGEST_EMAIL


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
    import sequences  # local import: sequences owns the safe auto-send defaults

    return {
        "tenant_id": tenant_id,
        "host_name": "",
        "units_json": "[]",
        "template": DEFAULT_TEMPLATE,
        "from_email": "",
        "reply_channels": "platform,email",
        "notify_webhook": "",
        "onboarded": "0",
        "automation_enabled": "0",
        "auto_steps": ",".join(sorted(sequences.default_enabled_steps())),
        "autopilot": "0",
        "check_times": DEFAULT_CHECK_TIMES,
        "last_check_at": "",
        "timezone": DEFAULT_TIMEZONE,
        "digest_enabled": "1",
        "digest_hour": DEFAULT_DIGEST_HOUR,
        "last_digest_at": "",
        "ingest_mode": INGEST_EMAIL,
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


def _clean_int(value) -> int | None:
    """Digits from a form field, or None. Tolerates '$3,200' and '2 guests'."""
    digits = re.sub(r"[^\d]", "", str(value or ""))
    return int(digits) if digits else None


def units_from_form(form) -> list[dict]:
    """Build the unit catalog from the Settings form's parallel field arrays.

    The catalog is still stored as JSON (no schema change), but owners now edit
    real labelled fields instead of hand-writing it — typing JSON was an
    adoption blocker, and one stray comma silently wiped the agent's facts.
    Rows with no name AND no area are treated as blank and dropped.
    """
    names = form.getlist("unit_name")
    areas = form.getlist("unit_area")
    prices = form.getlist("unit_price")
    guests = form.getlist("unit_guests")
    pets = form.getlist("unit_pets")
    min_n = form.getlist("unit_min_nights")
    max_n = form.getlist("unit_max_nights")
    notes = form.getlist("unit_notes")
    ids = form.getlist("unit_id")

    def at(seq, i):
        return seq[i] if i < len(seq) else ""

    units: list[dict] = []
    for i in range(len(names)):
        name = (at(names, i) or "").strip()
        area = (at(areas, i) or "").strip()
        if not name and not area:
            continue
        unit: dict = {
            "id": (at(ids, i) or "").strip() or f"unit{len(units) + 1}",
            "name": name or f"Unit {len(units) + 1}",
        }
        if area:
            unit["area"] = area
        # Ties a lead's listing title back to this unit (used by the responder).
        if name:
            unit["listing_match"] = name
        for key, seq in (("monthly_price", prices), ("max_occupancy", guests),
                         ("min_nights", min_n), ("max_nights", max_n)):
            val = _clean_int(at(seq, i))
            if val is not None:
                unit[key] = val
        choice = (at(pets, i) or "").strip().lower()
        if choice == "yes":
            unit["pets_allowed"] = True
        elif choice == "no":
            unit["pets_allowed"] = False
        note = (at(notes, i) or "").strip()
        if note:
            unit["notes"] = note
        units.append(unit)
    return units


def discover_units(tenant_id: str, site: str = "furnishedfinder") -> list[dict]:
    """Derive unit stubs from the properties already visible in this tenant's leads.

    Every lead names the listing it came from, so the catalog can be prefilled
    from data we already hold instead of asking the owner to retype their
    portfolio. Returns only units that aren't in the catalog yet; price,
    occupancy and house rules still need the owner (we never invent facts the
    agent would then quote at a guest).
    """
    import storage

    known = [str(u.get("name", "")) for u in get_units(tenant_id)]

    def already_have(name: str) -> bool:
        return any(_names_related(name, k) for k in known)

    items = storage.all_items(tenant_id, site)
    found: dict[str, dict] = {}

    # Scraped listings are the good source: they carry the real asking price,
    # the area and the photos. Leads only ever give us a name.
    for item in items.values():
        if item.get("kind") != "property":
            continue
        name = str(item.get("name") or item.get("title") or "").strip()
        if not name or already_have(name) or name.lower() in found:
            continue
        unit: dict = {
            "id": f"unit{len(found) + 1}",
            "name": name,
            "listing_match": name,
        }
        for key in ("area", "monthly_price", "images"):
            if item.get(key):
                unit[key] = item[key]
        if item.get("address"):
            unit["notes"] = f"Address on file: {item['address']}."
        found[name.lower()] = unit

    for item in items.values():
        if item.get("kind") != "lead":
            continue
        name, area = "", ""
        m = re.search(r"Property:?\s*\n?\s*(.+)", str(item.get("detail") or ""))
        if m:
            name = m.group(1).strip()
        parts = [p.strip() for p in str(item.get("title") or "").split("|")]
        if not name and parts:
            name = parts[0]
        # The row's second segment is the *listing's* location. (The detail's
        # "Location:" is where the tenant is travelling FROM — not the property.)
        if len(parts) > 1:
            area = parts[1]
        name = name.strip()
        if not name or already_have(name) or name.lower() in found:
            continue
        found[name.lower()] = {
            "id": f"unit{len(found) + 1}",
            "name": name,
            "listing_match": name,
            **({"area": area} if area else {}),
            **({"images": item["images"][:3]} if item.get("images") else {}),
        }
    return list(found.values())


def _name_key(value: str) -> str:
    """Normalized listing name for matching (case/punctuation/spacing-insensitive)."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _names_related(a: str, b: str) -> bool:
    """Whether two listing names refer to the same property.

    FurnishedFinder models a property with sub-units, so the catalog often holds
    "Quiet Spacious Home in NW DC - Unit 1" while the listings page calls the
    parent "Quiet Spacious Home in NW DC". Containment either way is the honest
    test — an exact match would miss every multi-unit host.
    """
    ka, kb = _name_key(a), _name_key(b)
    if not ka or not kb:
        return False
    return ka == kb or ka.startswith(kb) or kb.startswith(ka)


def scraped_listings(tenant_id: str, site: str = "furnishedfinder") -> list[dict]:
    """The tenant's FurnishedFinder listings, as scraped (kind='property')."""
    import storage

    return [
        item for item in storage.all_items(tenant_id, site).values()
        if item.get("kind") == "property"
    ]


# Facts we're willing to copy from a listing onto a unit. Deliberately short:
# these are the values the agent quotes verbatim to guests.
_ENRICHABLE = ("monthly_price", "area")


def suggested_enrichments(tenant_id: str, site: str = "furnishedfinder") -> list[dict]:
    """Facts a scraped listing could fill in on units that are missing them.

    Only ever *additive* — a value the host has already set is never proposed
    for overwrite, because their number beats the listing's.

    `shared` marks a listing that maps to more than one unit. That matters most
    for price: FurnishedFinder prices the whole property, so copying it onto
    each sub-unit would have the agent quoting the full-house rate for a single
    room. Those suggestions are surfaced but not pre-selected.
    """
    units = get_units(tenant_id)
    listings = scraped_listings(tenant_id, site)
    if not units or not listings:
        return []

    # How many units each listing covers, to spot the whole-property case.
    coverage: dict[str, int] = {}
    for listing in listings:
        name = str(listing.get("name") or listing.get("title") or "")
        coverage[name] = sum(1 for u in units
                             if _names_related(str(u.get("listing_match") or u.get("name") or ""), name))

    out: list[dict] = []
    for unit in units:
        label = str(unit.get("listing_match") or unit.get("name") or "")
        for listing in listings:
            name = str(listing.get("name") or listing.get("title") or "")
            if not _names_related(label, name):
                continue
            missing = {}
            for field in _ENRICHABLE:
                current = unit.get(field)
                # 0 / "" / None all mean "not set" in this catalog.
                if not current and listing.get(field):
                    missing[field] = listing[field]
            if missing:
                out.append({
                    "unit_id": unit.get("id"),
                    "unit_name": unit.get("name"),
                    "source": name,
                    "fields": missing,
                    "shared": coverage.get(name, 0) > 1,
                })
            break
    return out


def apply_enrichments(tenant_id: str, selections: list[str],
                      site: str = "furnishedfinder") -> int:
    """Apply chosen suggestions, each identified as "<unit_id>:<field>".

    Re-derives the suggestions server-side rather than trusting values posted by
    the browser, so a tampered form can't write an arbitrary price into the
    catalog the agent quotes from. Returns the number of fields written.
    """
    wanted = {s for s in selections if s}
    if not wanted:
        return 0
    allowed = {
        f"{s['unit_id']}:{field}": (s["unit_id"], field, value)
        for s in suggested_enrichments(tenant_id, site)
        for field, value in s["fields"].items()
    }
    units = get_units(tenant_id)
    by_id = {str(u.get("id")): u for u in units}
    applied = 0
    for key in wanted:
        match = allowed.get(key)
        if not match:
            continue
        unit_id, field, value = match
        unit = by_id.get(str(unit_id))
        if unit is not None and not unit.get(field):
            unit[field] = value
            applied += 1
    if applied:
        save_settings(tenant_id, units_json=json.dumps(units, indent=2))
    return applied


def unit_images(tenant_id: str, site: str = "furnishedfinder") -> dict[str, list[str]]:
    """Listing photos per unit name, harvested from the tenant's scraped leads.

    Photos live on the lead payloads (that's where the scraper sees them), so
    this joins them back onto the catalog by listing name rather than storing a
    second copy that could drift out of date.
    """
    import storage

    out: dict[str, list[str]] = {}
    for item in storage.all_items(tenant_id, site).values():
        images = item.get("images") or []
        if not images:
            continue
        # A scraped listing names itself directly.
        if item.get("kind") == "property":
            name = str(item.get("name") or item.get("title") or "").strip()
            if name:
                out.setdefault(name.lower(), images)
            continue
        name = ""
        m = re.search(r"Property:?\s*\n?\s*(.+)", str(item.get("detail") or ""))
        if m:
            name = m.group(1).strip()
        if not name:
            parts = [p.strip() for p in str(item.get("title") or "").split("|")]
            name = parts[0] if parts else ""
        if name:
            out.setdefault(name.strip().lower(), images)
    return out


def units_with_images(tenant_id: str, site: str = "furnishedfinder") -> list[dict]:
    """The unit catalog with any scraped listing photos attached (view-only)."""
    photos = unit_images(tenant_id, site)
    units = []
    for unit in get_units(tenant_id):
        unit = dict(unit)
        if not unit.get("images"):
            label = str(unit.get("listing_match") or unit.get("name") or "")
            for listing_name, images in photos.items():
                if _names_related(label, listing_name):
                    unit["images"] = images
                    break
        units.append(unit)
    return units


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
