"""Deal pipeline — the guest lifecycle, from first inquiry to arrival.

This is the model the product is organized around. A scraped lead/message is
not "an item to reply to"; it is a **deal** that moves through stages and has a
next action due at a specific time:

    new -> contacted -> nurturing -> booked -> pre_arrival -> staying -> completed
                                  \\-> lost

Why a separate table rather than more columns on `responses`: `responses` stores
one *drafting decision* per item (the agent's fit call + draft text). A deal
outlives any single draft — it accumulates contact history, a booking, and a
schedule of future automated touches. Keeping them apart means the existing
scrape/draft path is untouched while the lifecycle is layered on top.

Every row is scoped by `tenant_id` (and `site`, so the same guest inquiring on
two platforms stays two rows until a future merge step reconciles them).

Times are stored as ISO strings we control, so scheduling math happens in
Python and stays portable across SQLite and Postgres (same rationale as jobs.py).
"""
import re
from datetime import datetime, timedelta

import db

# --- Lifecycle stages -------------------------------------------------------
NEW = "new"                  # inquiry landed, nothing sent yet
CONTACTED = "contacted"      # we replied; waiting on the guest
NURTURING = "nurturing"      # follow-ups in flight after silence
BOOKED = "booked"            # owner confirmed the booking
PRE_ARRIVAL = "pre_arrival"  # booked and arrival is approaching
STAYING = "staying"          # checked in
COMPLETED = "completed"      # stay finished
LOST = "lost"                # went cold or declined

OPEN_STAGES = (NEW, CONTACTED, NURTURING)
BOOKED_STAGES = (BOOKED, PRE_ARRIVAL, STAYING)

STAGE_LABELS = {
    NEW: "New inquiry",
    CONTACTED: "Contacted",
    NURTURING: "Following up",
    BOOKED: "Booked",
    PRE_ARRIVAL: "Arriving soon",
    STAYING: "In stay",
    COMPLETED: "Completed",
    LOST: "Lost",
}

# A lead stops being realistically winnable after this long unanswered; past it
# we keep the deal but drop it out of the "needs you now" queue so a six-month
# old inquiry can never outrank this morning's.
LIVE_WINDOW_DAYS = 14

_COLS = (
    "id", "tenant_id", "site", "item_id", "kind", "stage", "guest_name",
    "unit_id", "check_in", "check_out", "nights", "monthly_value",
    "inquiry_at", "first_reply_at", "last_contact_at",
    "sequence", "step_index", "next_action_at", "next_action_step",
    "auto_send", "created_at", "updated_at",
)

_SELECT = f"SELECT {', '.join(_COLS)} FROM deals"


def _conn() -> db.Conn:
    c = db.connect()
    c.execute(
        """CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            site TEXT NOT NULL,
            item_id TEXT NOT NULL,
            kind TEXT,
            stage TEXT NOT NULL DEFAULT 'new',
            guest_name TEXT,
            unit_id TEXT,
            check_in TEXT,
            check_out TEXT,
            nights INTEGER,
            monthly_value INTEGER,
            inquiry_at TEXT,
            first_reply_at TEXT,
            last_contact_at TEXT,
            sequence TEXT,
            step_index INTEGER DEFAULT 0,
            next_action_at TEXT,
            next_action_step TEXT,
            auto_send INTEGER NOT NULL DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        )"""
    )
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS deals_tenant_item "
        "ON deals (tenant_id, site, item_id)"
    )
    return c


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row(row) -> dict | None:
    return dict(zip(_COLS, row)) if row else None


# --- Date parsing -----------------------------------------------------------
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(value: str | None) -> str | None:
    """Normalize the date shapes FurnishedFinder emits into ISO `YYYY-MM-DD`.

    Handles the row style ('7/18/26', '9/1/2026'), the detail style
    ('July 18, 2026', 'Feb. 10, 2026') and the message-list style ('Jul. 18',
    which carries no year — assumed to be the current one). Returns None when
    nothing parses, so callers can degrade rather than guess.
    """
    if not value:
        return None
    s = str(value).strip()

    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", s)
    if m:
        mon, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        try:
            return datetime(year, mon, day).date().isoformat()
        except ValueError:
            return None

    m = re.match(r"^([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{1,2})(?:,?\s*(\d{4}))?$", s)
    if m:
        mon = _MONTHS.get(m.group(1).lower())
        if not mon:
            return None
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else datetime.now().year
        try:
            return datetime(year, mon, day).date().isoformat()
        except ValueError:
            return None
    return None


# "Date received: July 18, 2026" as it appears in a lead's detail text. The site
# adapter now lifts this into `received_at` at scrape time; this recovers it from
# detail text captured before it did, so existing installs don't need a re-scrape.
_RECEIVED_RE = re.compile(
    r"Date\s+received:?\s*\n?\s*([A-Za-z]{3}[a-z]*\.?\s+\d{1,2},?\s*\d{4})", re.I
)


def inquiry_date(item: dict) -> str | None:
    """When the guest actually asked, as ISO — or None if we genuinely can't tell.

    Prefers the explicit detail-page value over the row-derived `received`,
    which can be the move-OUT date (see furnishedfinder._parse_lead_detail).
    Anything in the future is rejected outright: it cannot be an arrival time,
    and trusting it would set every urgency clock years ahead.
    """
    direct = parse_date(item.get("received_at"))
    if direct:
        return direct
    m = _RECEIVED_RE.search(str(item.get("detail") or ""))
    if m:
        recovered = parse_date(m.group(1))
        if recovered:
            return recovered
    candidate = parse_date(item.get("received") or item.get("date"))
    today = datetime.now().date().isoformat()
    return candidate if (candidate and candidate <= today) else None


def _to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def age_hours(value: str | None) -> float | None:
    dt = _to_dt(value)
    if dt is None:
        return None
    return (datetime.now() - dt).total_seconds() / 3600.0


def humanize_age(value: str | None) -> str:
    """'2h' / '3d' / '—' — compact enough for a list chip."""
    h = age_hours(value)
    if h is None:
        return "—"
    if h < 1:
        return f"{max(1, int(h * 60))}m"
    if h < 48:
        return f"{int(h)}h"
    return f"{int(h / 24)}d"


# --- Deriving a deal from a scraped item ------------------------------------
def _estimate_value(item: dict, units: list[dict] | None, unit_id: str | None) -> int:
    """Rough monthly value, used only to size the pipeline for the owner.

    Prefers the matched unit's catalog price; falls back to a budget figure
    stated in the inquiry ('Up to $3,200'). Returns 0 when neither is known —
    the UI shows nothing rather than inventing a number.
    """
    for u in units or []:
        if unit_id and str(u.get("id")) == str(unit_id):
            price = u.get("monthly_price") or 0
            if isinstance(price, (int, float)) and price > 0:
                return int(price)
    budget = str(item.get("budget") or "")
    m = re.search(r"\$\s?([\d,]{3,})", budget)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            return 0
    return 0


def derive(item: dict, response: dict | None, units: list[dict] | None = None) -> dict:
    """Map a scraped lead/message (+ the agent's decision) onto deal fields."""
    kind = item.get("kind", "lead")
    guest = (item.get("traveler") or item.get("sender") or "").strip()
    unit_id = (response or {}).get("unit_id")
    inquiry = inquiry_date(item)
    return {
        "kind": kind,
        "guest_name": guest,
        "unit_id": unit_id,
        "check_in": parse_date(item.get("move_in")),
        "check_out": parse_date(item.get("move_out")),
        "nights": item.get("nights") if isinstance(item.get("nights"), int) else None,
        "monthly_value": _estimate_value(item, units, unit_id),
        # Fall back to "now" so a lead whose date we couldn't parse still gets a
        # sane clock rather than sorting as infinitely old.
        "inquiry_at": f"{inquiry}T09:00:00" if inquiry else _now(),
    }


def ensure(tenant_id: str, site: str, item: dict, response: dict | None = None,
           units: list[dict] | None = None) -> dict:
    """Create the deal for a scraped item, or refresh its derived facts.

    Idempotent: re-running a scrape updates the guest/unit/date facts but never
    resets `stage`, the contact history, or the automation schedule.
    """
    tenant_id, item_id = str(tenant_id), str(item["id"])
    fields = derive(item, response, units)
    existing = get(tenant_id, site, item_id)
    now = _now()
    if existing:
        # Repair a clock set from a bad `received` value. Two cases heal here:
        # an impossible future date (the old row-based parse), and a stored date
        # superseded by a trustworthy `received_at` from the lead detail page.
        inquiry = existing.get("inquiry_at")
        if inquiry_date(item) or (inquiry and str(inquiry) > now):
            inquiry = fields["inquiry_at"]
        with _conn() as c:
            c.execute(
                """UPDATE deals SET guest_name=?, unit_id=?, check_in=?, check_out=?,
                       nights=?, monthly_value=?, inquiry_at=?, updated_at=?
                   WHERE tenant_id=? AND site=? AND item_id=?""",
                (fields["guest_name"], fields["unit_id"], fields["check_in"],
                 fields["check_out"], fields["nights"], fields["monthly_value"],
                 inquiry, now, tenant_id, site, item_id),
            )
        return get(tenant_id, site, item_id)

    with _conn() as c:
        c.execute(
            """INSERT INTO deals (tenant_id, site, item_id, kind, stage, guest_name,
                   unit_id, check_in, check_out, nights, monthly_value, inquiry_at,
                   sequence, step_index, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tenant_id, site, item_id, fields["kind"], NEW, fields["guest_name"],
             fields["unit_id"], fields["check_in"], fields["check_out"],
             fields["nights"], fields["monthly_value"], fields["inquiry_at"],
             "presale", 0, now, now),
        )
    return get(tenant_id, site, item_id)


def backfill(tenant_id: str, site: str, items: dict[str, dict],
             responses: dict[str, dict], units: list[dict] | None = None) -> int:
    """Open deals for stored items that predate the pipeline. Returns how many.

    Runs from the dashboard read path so an existing install gains the lifecycle
    without a migration step. It only writes for items with no deal yet, so the
    steady-state cost is one SELECT.
    """
    existing = by_item(tenant_id, site)
    now = _now()
    created = 0
    for item_id, item in items.items():
        deal = existing.get(item_id)
        if deal is not None:
            # Already open — re-derive only when the stored clock is wrong:
            # either impossible (a future "inquiry" date, from the old row-based
            # parsing) or superseded by a trustworthy `received_at` that a later
            # detail scrape backfilled onto the item. Otherwise leave it alone,
            # so the steady-state cost of this pass stays one SELECT.
            stored = str(deal.get("inquiry_at") or "")
            truth = inquiry_date(item)
            if stored > now or (truth and stored[:10] != truth):
                ensure(tenant_id, site, {**item, "id": item_id},
                       responses.get(item_id), units=units)
            continue
        item = {**item, "id": item_id}
        deal = ensure(tenant_id, site, item, responses.get(item_id), units=units)
        # A reply already went out before the pipeline existed: reflect that so
        # the deal doesn't reappear in "needs you" and skew response metrics.
        resp = responses.get(item_id) or {}
        if deal and resp.get("status") == "sent":
            update(tenant_id, site, item_id, stage=CONTACTED,
                   first_reply_at=resp.get("sent_at"),
                   last_contact_at=resp.get("sent_at"))
        created += 1
    return created


# --- Reads ------------------------------------------------------------------
def get(tenant_id: str, site: str, item_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            f"{_SELECT} WHERE tenant_id=? AND site=? AND item_id=?",
            (str(tenant_id), site, str(item_id)),
        ).fetchone()
    return _row(row)


def tenants_with_due(now_iso: str | None = None) -> list[str]:
    """Tenants that have at least one lifecycle step due — the worker's work list."""
    now_iso = now_iso or _now()
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT tenant_id FROM deals "
            "WHERE next_action_at IS NOT NULL AND next_action_at <= ? "
            "AND stage NOT IN (?, ?)",
            (now_iso, LOST, COMPLETED),
        ).fetchall()
    return [str(r[0]) for r in rows]


def all_deals(tenant_id: str, site: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            f"{_SELECT} WHERE tenant_id=? AND site=? ORDER BY id DESC",
            (str(tenant_id), site),
        ).fetchall()
    return [_row(r) for r in rows]


def by_item(tenant_id: str, site: str) -> dict[str, dict]:
    """All deals keyed by item_id, for joining onto the scraped item list."""
    return {d["item_id"]: d for d in all_deals(tenant_id, site)}


# --- Writes -----------------------------------------------------------------
def update(tenant_id: str, site: str, item_id: str, **fields) -> None:
    allowed = {
        "stage", "guest_name", "unit_id", "check_in", "check_out", "nights",
        "monthly_value", "first_reply_at", "last_contact_at", "sequence",
        "step_index", "next_action_at", "next_action_step", "auto_send",
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    assignments = ", ".join(f"{k}=?" for k in sets)
    vals = list(sets.values()) + [_now(), str(tenant_id), site, str(item_id)]
    with _conn() as c:
        c.execute(
            f"UPDATE deals SET {assignments}, updated_at=? "
            "WHERE tenant_id=? AND site=? AND item_id=?",
            vals,
        )


def record_contact(tenant_id: str, site: str, item_id: str) -> None:
    """Mark that we just sent the guest something.

    Stamps `first_reply_at` once (it powers the response-time metric, which is
    the product's core speed claim) and moves a brand-new deal to `contacted`.
    """
    deal = get(tenant_id, site, item_id)
    if not deal:
        return
    now = _now()
    fields: dict = {"last_contact_at": now}
    if not deal.get("first_reply_at"):
        fields["first_reply_at"] = now
    if deal.get("stage") == NEW:
        fields["stage"] = CONTACTED
    update(tenant_id, site, item_id, **fields)


def mark_booked(tenant_id: str, site: str, item_id: str,
                check_in: str | None = None, check_out: str | None = None) -> None:
    """Owner confirms the booking — this is what starts the pre-arrival agent.

    Booking can't be reliably detected by scraping (it happens off-platform, in
    a lease or a call), so it stays an explicit one-click human signal.
    """
    fields: dict = {"stage": BOOKED, "sequence": "prearrival", "step_index": 0,
                    "next_action_at": None, "next_action_step": None}
    if check_in:
        fields["check_in"] = check_in
    if check_out:
        fields["check_out"] = check_out
    update(tenant_id, site, item_id, **fields)


def mark_lost(tenant_id: str, site: str, item_id: str) -> None:
    update(tenant_id, site, item_id, stage=LOST,
           next_action_at=None, next_action_step=None)


# --- Views the dashboard is built from --------------------------------------
def is_draft_failure(response: dict | None) -> bool:
    """A 'skipped' row that records a drafting *error*, not an agent decision.

    These still need a human (or a re-draft); a genuine "not a fit" skip does not.
    """
    resp = response or {}
    return (resp.get("status") == "skipped"
            and str(resp.get("reason") or "").lower().startswith("draft error"))


def needs_action(deals: list[dict], responses: dict[str, dict]) -> list[dict]:
    """Deals waiting on the human, most-at-risk first.

    Replaces the old hash-ordered list. A deal qualifies when the guest is still
    realistically winnable (inside LIVE_WINDOW_DAYS) and nobody has resolved it:
    a draft is ready to approve, drafting failed, or the agent hasn't looked yet.
    Deliberately excluded are deals the agent cleanly *skipped* — a wrong-city
    lead is handled, and burying five of those in the queue is what makes an
    inbox feel like work. Those surface in `reviewable()` instead.
    """
    out = []
    for d in deals:
        if d.get("stage") not in OPEN_STAGES:
            continue
        resp = responses.get(d["item_id"])
        status = (resp or {}).get("status")
        if status in ("sent", "dismissed"):
            continue
        if status == "skipped" and not is_draft_failure(resp):
            continue
        age = age_hours(d.get("inquiry_at"))
        if age is not None and age > LIVE_WINDOW_DAYS * 24:
            continue
        out.append(d)
    out.sort(key=lambda d: d.get("inquiry_at") or "")
    return out


def reviewable(deals: list[dict], responses: dict[str, dict]) -> list[dict]:
    """Deals the agent skipped on purpose — shown for oversight, not as work."""
    out = []
    for d in deals:
        resp = responses.get(d["item_id"])
        if (resp or {}).get("status") == "skipped" and not is_draft_failure(resp):
            if d.get("stage") in OPEN_STAGES:
                out.append(d)
    out.sort(key=lambda d: d.get("inquiry_at") or "", reverse=True)
    return out


def scheduled(deals: list[dict]) -> list[dict]:
    """Deals with an automated step queued, soonest first."""
    out = [d for d in deals if d.get("next_action_at")
           and d.get("stage") not in (LOST, COMPLETED)]
    out.sort(key=lambda d: d["next_action_at"])
    return out


def arrivals(deals: list[dict], within_days: int = 30) -> list[dict]:
    """Booked guests arriving soon — the post-booking half of the lifecycle."""
    today = datetime.now().date().isoformat()
    horizon = (datetime.now() + timedelta(days=within_days)).date().isoformat()
    out = [d for d in deals
           if d.get("stage") in BOOKED_STAGES
           and d.get("check_in") and today <= d["check_in"] <= horizon]
    out.sort(key=lambda d: d["check_in"])
    return out


def metrics(deals: list[dict], responses: dict[str, dict]) -> dict:
    """Headline numbers for the dashboard KPI strip.

    `median_response` is deliberately the median, not the mean: one lead you
    left for a week shouldn't make an otherwise-fast operation look broken.
    """
    open_deals = [d for d in deals if d.get("stage") in OPEN_STAGES]
    booked = [d for d in deals if d.get("stage") in BOOKED_STAGES]

    response_hours = []
    for d in deals:
        start, reply = _to_dt(d.get("inquiry_at")), _to_dt(d.get("first_reply_at"))
        if start and reply and reply >= start:
            response_hours.append((reply - start).total_seconds() / 3600.0)
    response_hours.sort()
    median = response_hours[len(response_hours) // 2] if response_hours else None

    contacted = [d for d in deals if d.get("first_reply_at")]
    return {
        "needs_action": len(needs_action(deals, responses)),
        "open_count": len(open_deals),
        "pipeline_value": sum(int(d.get("monthly_value") or 0) for d in open_deals),
        "booked_count": len(booked),
        "arrivals_30d": len(arrivals(deals)),
        "median_response": median,
        "median_response_label": _fmt_hours(median),
        "conversion": (len(booked) / len(contacted) * 100) if contacted else None,
        "scheduled_count": len(scheduled(deals)),
    }


def _fmt_hours(h: float | None) -> str:
    if h is None:
        return "—"
    if h < 1:
        return f"{int(h * 60)}m"
    if h < 48:
        return f"{h:.1f}h".replace(".0h", "h")
    return f"{int(h / 24)}d"
