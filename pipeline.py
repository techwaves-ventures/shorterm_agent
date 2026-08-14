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
    "thread_key", "last_guest_reply_at", "notes", "closed_reason",
)

_SELECT = f"SELECT {', '.join(_COLS)} FROM deals"

# Columns added after the deals table shipped. Applied as idempotent ALTERs so an
# existing install gains them without a migration step (same posture as
# storage._migrate_tenant_id).
_ADDED_COLS = (
    ("thread_key", "TEXT"),
    ("last_guest_reply_at", "TEXT"),
    ("notes", "TEXT"),
    ("closed_reason", "TEXT"),
)


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
            updated_at TEXT,
            thread_key TEXT,
            last_guest_reply_at TEXT,
            notes TEXT,
            closed_reason TEXT
        )"""
    )
    have = db.table_columns(c, "deals")
    for col, decl in _ADDED_COLS:
        if col not in have:
            c.execute(f"ALTER TABLE deals ADD COLUMN {col} {decl}")
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS deals_tenant_item "
        "ON deals (tenant_id, site, item_id)"
    )
    # The inbox filters and sorts on these; without indexes every page load is a
    # full scan of the tenant's deals.
    c.execute(
        "CREATE INDEX IF NOT EXISTS deals_thread "
        "ON deals (tenant_id, site, thread_key)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS deals_tenant_stage "
        "ON deals (tenant_id, site, stage, inquiry_at)"
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


# --- Thread identity --------------------------------------------------------
# Deals key on (tenant_id, site, item_id), and every inbound item has its own
# id. Left alone that means a guest's reply opens a *second* deal beside the one
# it is answering: the history splits, the owner sees the same person twice, and
# the nurture sequence on the original keeps firing at someone who already
# wrote back. `thread_key` is the stable "who is this conversation with" handle
# that lets an inbound message find the deal it belongs to.
_THREAD_STRIP = re.compile(r"[^a-z0-9]+")


def thread_key(item: dict) -> str:
    """Stable identity for the conversation an item belongs to.

    Guest name plus property, both aggressively normalized: FurnishedFinder
    renders the same traveler as "Emma M.", "Emma M", and "emma m." across its
    lead email, message email and detail page, and a key that treats those as
    three people would defeat the purpose. Property is included because the same
    traveler enquiring about two different units is genuinely two conversations.

    Returns "" when there is no name to key on — callers must treat an empty key
    as "no thread", never as a key that matches other empty ones.
    """
    guest = (item.get("traveler") or item.get("sender") or item.get("guest_name") or "")
    prop = (item.get("property_name") or item.get("unit_id") or "")
    name = _THREAD_STRIP.sub("", guest.lower())
    if not name:
        return ""
    return f"{name}|{_THREAD_STRIP.sub('', str(prop).lower())}"


def find_thread(tenant_id: str, site: str, key: str,
                exclude_item_id: str | None = None) -> dict | None:
    """The open deal this conversation already has, if any.

    Closed deals are excluded: a guest writing in months after a stay ended is
    starting a new conversation, not continuing a finished one. Newest first, so
    a repeat guest attaches to their current deal rather than an ancient one.

    Falls back on the guest alone when — and only when — one of the two sides
    has no property to key on. The ingest paths don't always agree about the
    property (a notification email names it, a scraped row often doesn't), so
    requiring both halves to match would orphan the reply this exists to
    rescue. But falling back whenever the exact key misses is worse: a guest
    asking about Unit B would be swallowed into their open Unit A deal, which
    the key's own definition says is a different conversation.

    So: an unqualified incoming item may attach to any of that guest's open
    deals, and a qualified one may attach only to a deal that is itself
    unqualified. Two qualified keys that disagree are two conversations.
    """
    if not key:
        return None
    exact = _find_thread_where(tenant_id, site, "thread_key=?", [key],
                               exclude_item_id)
    if exact:
        return exact
    guest, _, prop = key.partition("|")
    if not guest:
        return None
    if not prop:
        # We don't know which unit this is about — any open deal for this guest.
        return _find_thread_where(tenant_id, site, "thread_key LIKE ?",
                                  [guest + "|%"], exclude_item_id)
    # We know the unit; only join a deal that never recorded one.
    return _find_thread_where(tenant_id, site, "thread_key=?",
                              [guest + "|"], exclude_item_id)


def _find_thread_where(tenant_id: str, site: str, clause: str, params: list,
                       exclude_item_id: str | None) -> dict | None:
    args: list = [str(tenant_id), site] + params + [LOST, COMPLETED]
    sql = (f"{_SELECT} WHERE tenant_id=? AND site=? AND {clause} "
           "AND stage NOT IN (?, ?)")
    if exclude_item_id:
        sql += " AND item_id<>?"
        args.append(str(exclude_item_id))
    sql += " ORDER BY id DESC"
    with _conn() as c:
        row = c.execute(sql, args).fetchone()
    return _row(row)


def thread_items(key: str, items: dict[str, dict]) -> list[dict]:
    """Every stored inbound item belonging to one conversation, oldest first.

    Same rule as `find_thread`, and for the same reason: the guest must match,
    and the property may differ only when one side didn't state one. Matching on
    the guest alone would render another unit's messages inside this deal's
    conversation — the operator would read someone's Unit B enquiry as part of
    their Unit A thread, and reply to the wrong one.
    """
    if not key:
        return []
    guest, _, prop = key.partition("|")
    if not guest:
        return []
    out = []
    for it in items.values():
        item_guest, _, item_prop = thread_key(it).partition("|")
        if not item_guest or item_guest != guest:
            continue
        if prop and item_prop and prop != item_prop:
            continue
        out.append(it)
    out.sort(key=lambda it: str(it.get("first_seen") or ""))
    return out


def record_guest_reply(tenant_id: str, site: str, item_id: str,
                       at: str | None = None) -> dict | None:
    """A guest wrote back. Stamp it and stand the follow-up machine down.

    `last_contact_at` only ever recorded *our* sends, so nothing in the system
    could tell "silent for four days" from "answered us an hour ago" — nurture
    steps kept firing at people who had already replied. Cancelling the pending
    step is the point: the guest's message supersedes whatever we had queued,
    and the deal moves to the owner's "needs you" queue instead.
    """
    deal = get(tenant_id, site, item_id)
    if not deal:
        return None
    fields: dict = {"last_guest_reply_at": at or _now(),
                    "next_action_at": None, "next_action_step": None}
    # Nurturing means "chasing silence" — a reply ends that, but we don't touch
    # a booked/pre-arrival deal's stage, where follow-ups are logistics not chase.
    if deal.get("stage") in (NEW, CONTACTED, NURTURING):
        fields["stage"] = CONTACTED
    update(tenant_id, site, item_id, **fields)
    return get(tenant_id, site, item_id)


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
        "thread_key": thread_key(item),
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
        # An existing deal keeps the thread it was filed under unless it never
        # had one (a row created before thread_key existed) — re-keying a live
        # conversation would strand the messages already attached to it.
        key = existing.get("thread_key") or fields["thread_key"]
        with _conn() as c:
            c.execute(
                """UPDATE deals SET guest_name=?, unit_id=?, check_in=?, check_out=?,
                       nights=?, monthly_value=?, inquiry_at=?, thread_key=?,
                       updated_at=?
                   WHERE tenant_id=? AND site=? AND item_id=?""",
                (fields["guest_name"], fields["unit_id"], fields["check_in"],
                 fields["check_out"], fields["nights"], fields["monthly_value"],
                 inquiry, key, now, tenant_id, site, item_id),
            )
        return get(tenant_id, site, item_id)

    with _conn() as c:
        c.execute(
            """INSERT INTO deals (tenant_id, site, item_id, kind, stage, guest_name,
                   unit_id, check_in, check_out, nights, monthly_value, inquiry_at,
                   sequence, step_index, thread_key, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tenant_id, site, item_id, fields["kind"], NEW, fields["guest_name"],
             fields["unit_id"], fields["check_in"], fields["check_out"],
             fields["nights"], fields["monthly_value"], fields["inquiry_at"],
             "presale", 0, fields["thread_key"], now, now),
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
        # A message belonging to a conversation we already have joins it rather
        # than opening a second deal — the same rule the live ingest path uses.
        # Without this, backfilling a mailbox re-creates exactly the duplicate
        # deals that threading exists to prevent.
        if item.get("kind") == "message":
            parent = find_thread(tenant_id, site, thread_key(item),
                                 exclude_item_id=item_id)
            if parent:
                # Only stamp a reply *newer* than the one already recorded.
                # A threaded message never gets a deal row of its own, so it is
                # never in `existing` and this branch re-runs on every backfill
                # — and backfill runs on every dashboard load. Re-stamping
                # unconditionally would re-clear next_action_at each time, so
                # any deal that had ever received a guest message could never
                # hold a scheduled follow-up again. That is the opposite of what
                # threading is for.
                at = str(item.get("first_seen") or "")
                seen_at = str(parent.get("last_guest_reply_at") or "")
                if at and at > seen_at:
                    record_guest_reply(tenant_id, site, parent["item_id"], at=at)
                elif not at and not seen_at:
                    record_guest_reply(tenant_id, site, parent["item_id"])
                continue
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


def tenants_with_deals() -> list[str]:
    """Every tenant that has a deal at all.

    `tenants_with_due` only finds tenants with a *scheduled* step, which is the
    wrong work list for the lifecycle sweep: a stay that needs closing has no
    next action by definition, so those tenants would never be visited.
    """
    with _conn() as c:
        rows = c.execute("SELECT DISTINCT tenant_id FROM deals").fetchall()
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
        "thread_key", "last_guest_reply_at", "notes", "closed_reason",
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


def mark_lost(tenant_id: str, site: str, item_id: str,
              reason: str | None = None) -> None:
    update(tenant_id, site, item_id, stage=LOST, closed_reason=reason,
           next_action_at=None, next_action_step=None)


# A deal nobody has touched for this long, with no follow-ups left to send, is
# over. Deliberately well past LIVE_WINDOW_DAYS (which only drops a lead out of
# the "needs you now" queue): dropping out of the queue is a display decision and
# recoverable, closing the deal is a lifecycle decision and shows as a loss.
STALE_CLOSE_DAYS = 21

# Booked deals enter the pre-arrival stream this far ahead of check-in.
PRE_ARRIVAL_DAYS = 7


def advance_lifecycle(tenant_id: str, site: str, today: str | None = None) -> dict:
    """Move deals through the stages that a calendar — not a human — decides.

    `STAYING` and `COMPLETED` were declared, filtered on and labelled, but no
    code path ever wrote them: a booked guest stayed "booked" through their
    arrival, their stay and their checkout, and `mark_lost` was reachable only
    from a human click. So the pipeline had no terminal state that didn't
    require someone to remember to press something, and "closed" was never true
    of anything the owner hadn't personally closed.

    Returns a per-transition count. Idempotent: re-running on the same day is a
    no-op, so it is safe to call from every worker pass.
    """
    today = today or datetime.now().date().isoformat()
    horizon = (datetime.fromisoformat(today).date()
               + timedelta(days=PRE_ARRIVAL_DAYS)).isoformat()
    stale_before = (datetime.fromisoformat(today) - timedelta(days=STALE_CLOSE_DAYS)
                    ).isoformat(timespec="seconds")
    moved = {"pre_arrival": 0, "staying": 0, "completed": 0, "lost": 0}

    for deal in all_deals(tenant_id, site):
        stage = deal.get("stage")
        item_id = deal["item_id"]
        check_in, check_out = deal.get("check_in"), deal.get("check_out")

        # Checkout is the strongest signal and is checked first, for every
        # booked stage and without requiring a check-in. Gating this on
        # check_in — as an earlier version did — left a booking recorded with
        # only a check-out date stuck in `booked` forever, permanently
        # inflating booked_count and the arrivals list.
        if stage in BOOKED_STAGES and check_out and check_out < today:
            update(tenant_id, site, item_id, stage=COMPLETED,
                   next_action_at=None, next_action_step=None)
            moved["completed"] += 1
        elif stage in (BOOKED, PRE_ARRIVAL) and check_in and check_in <= today:
            update(tenant_id, site, item_id, stage=STAYING)
            moved["staying"] += 1
        elif stage == BOOKED and check_in and check_in <= horizon:
            update(tenant_id, site, item_id, stage=PRE_ARRIVAL)
            moved["pre_arrival"] += 1
        elif stage in OPEN_STAGES:
            if _is_abandoned(deal, stale_before):
                mark_lost(tenant_id, site, item_id,
                          reason=f"No reply for {STALE_CLOSE_DAYS} days")
                moved["lost"] += 1
    return moved


def _is_abandoned(deal: dict, stale_before: str) -> bool:
    """Whether an open deal has gone cold with nothing left to try.

    Requires the sequence to be *exhausted* (`next_action_at` cleared), not just
    quiet — closing a deal that still has a follow-up queued would cancel the
    very message that might have won it. And a deal where the guest is the last
    one to have spoken is never abandoned; that one is waiting on us.
    """
    if deal.get("next_action_at"):
        return False
    if _guest_is_waiting(deal):
        return False
    last = max(str(deal.get("last_guest_reply_at") or ""),
               str(deal.get("last_contact_at") or ""),
               str(deal.get("inquiry_at") or ""))
    return bool(last) and last < stale_before


# --- The one state the operator actually thinks in --------------------------
# "Open / closed / responded" was previously smeared across three tables that
# could disagree: deals.stage, responses.status and outbox.status. Anything that
# wanted to answer "what still needs me?" had to re-derive it, and the UI and the
# automation each did so slightly differently. `lead_state` is the single answer,
# consumed by both, so they cannot drift apart.
NEEDS_YOU = "needs_you"            # a draft to approve, a failed send, or unlooked-at
GUEST_REPLIED = "guest_replied"    # they wrote back after our last message
AWAITING_GUEST = "awaiting_guest"  # we replied; the ball is with them
SCHEDULED = "scheduled"            # an automated step is queued
CLOSED = "closed"                  # booked, completed, lost or dismissed

LEAD_STATES = (NEEDS_YOU, GUEST_REPLIED, AWAITING_GUEST, SCHEDULED, CLOSED)

LEAD_STATE_LABELS = {
    NEEDS_YOU: "Needs you",
    GUEST_REPLIED: "Guest replied",
    AWAITING_GUEST: "Awaiting guest",
    SCHEDULED: "Scheduled",
    CLOSED: "Closed",
}

CLOSED_STAGES = (BOOKED, PRE_ARRIVAL, STAYING, COMPLETED, LOST)


def lead_state(deal: dict, response: dict | None = None,
               has_failed_send: bool = False) -> str:
    """Which of the five states this deal is in, most-urgent interpretation first.

    `guest_replied` deliberately outranks `needs_you`: both want the owner, but a
    person who is waiting on an answer right now outranks a draft that has been
    sitting patiently. `scheduled` outranks `awaiting_guest` because "we will
    chase them on Thursday" is more informative than "they're quiet".
    """
    resp = response or {}
    status = resp.get("status")

    if deal.get("stage") in CLOSED_STAGES or status == "dismissed":
        return CLOSED
    if _guest_is_waiting(deal):
        return GUEST_REPLIED
    if has_failed_send or is_draft_failure(resp):
        return NEEDS_YOU
    if status == "draft" or status is None:
        return NEEDS_YOU
    if status == "skipped":
        # The agent decided not to pursue it and said why — handled, not work.
        return CLOSED
    if deal.get("next_action_at"):
        return SCHEDULED
    return AWAITING_GUEST


def _guest_is_waiting(deal: dict) -> bool:
    """True when the guest's last message came after our last one."""
    replied = str(deal.get("last_guest_reply_at") or "")
    if not replied:
        return False
    return replied > str(deal.get("last_contact_at") or "")


def state_counts(rows: list[dict]) -> dict[str, int]:
    """Per-state totals for the inbox tab bar. `rows` carry a `state` key."""
    counts = {s: 0 for s in LEAD_STATES}
    for r in rows:
        state = r.get("state")
        if state in counts:
            counts[state] += 1
    counts["all"] = len(rows)
    return counts


# --- The inbox query --------------------------------------------------------
# The dashboard's read path funnels through storage.all_items(), which loads
# every stored payload for the tenant into memory and filters in Python. That is
# fine for six hard-coded sections; it is not fine for a filterable list, where
# the whole point is that the operator can ask for one slice of a large mailbox.
# So the inbox filters, counts and paginates in SQL over the small columns, and
# the caller loads payloads for only the page it is about to render.
#
# `_STATE_SQL` mirrors `lead_state()` branch for branch. Two implementations of
# one rule is exactly the drift this workstream exists to remove, so
# `test_inbox_filters.py` asserts they agree across a matrix of deals — that
# test is what keeps them honest.
def _state_sql(failed_count: int) -> str:
    failed = (f"d.item_id IN ({','.join('?' * failed_count)})"
              if failed_count else "1=0")
    return f"""CASE
        WHEN d.stage IN ('{BOOKED}','{PRE_ARRIVAL}','{STAYING}','{COMPLETED}','{LOST}')
            THEN '{CLOSED}'
        WHEN r.status = 'dismissed' THEN '{CLOSED}'
        WHEN d.last_guest_reply_at IS NOT NULL
             AND d.last_guest_reply_at > COALESCE(d.last_contact_at, '')
            THEN '{GUEST_REPLIED}'
        WHEN {failed} THEN '{NEEDS_YOU}'
        WHEN r.status = 'skipped'
             AND LOWER(COALESCE(r.reason, '')) LIKE 'draft error%' THEN '{NEEDS_YOU}'
        WHEN r.status = 'draft' OR r.status IS NULL THEN '{NEEDS_YOU}'
        WHEN r.status = 'skipped' THEN '{CLOSED}'
        WHEN d.next_action_at IS NOT NULL THEN '{SCHEDULED}'
        ELSE '{AWAITING_GUEST}'
    END"""


_RESPONSE_COLS = ("status", "unit_id", "reason", "draft", "confidence",
                  "created_at", "sent_at")

DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 100


def inbox_page(tenant_id: str, site: str, *, state: str | None = None,
               kind: str | None = None, unit: str | None = None,
               q: str | None = None, page: int = 1,
               per_page: int = DEFAULT_PER_PAGE,
               failed_item_ids: tuple = ()) -> dict:
    """One page of the inbox, plus the per-state counts for the tab bar.

    `failed_item_ids` is supplied by the caller rather than joined here: a failed
    send lives in the outbox, which is a layer above this one, and failures are
    rare enough that passing the (small) id set in beats a correlated subquery.

    Counts are computed over the *unpaginated* filtered set — the tab bar has to
    say how many are in each state, not how many are on this page.
    """
    tenant_id, site = str(tenant_id), site
    failed = [str(i) for i in failed_item_ids]
    page = max(1, int(page or 1))
    per_page = max(1, min(int(per_page or DEFAULT_PER_PAGE), MAX_PER_PAGE))

    state_expr = _state_sql(len(failed))
    where = ["d.tenant_id=?", "d.site=?"]
    # The CASE sits in the SELECT list, so its parameters bind before the WHERE
    # clause's — order here is load-bearing.
    args: list = list(failed) + [tenant_id, site]
    if kind in ("lead", "message"):
        where.append("d.kind=?")
        args.append(kind)
    if unit:
        where.append("d.unit_id=?")
        args.append(str(unit))
    if q:
        where.append("(LOWER(COALESCE(d.guest_name,'')) LIKE ? "
                     "OR LOWER(COALESCE(d.thread_key,'')) LIKE ?)")
        needle = f"%{q.strip().lower()}%"
        args += [needle, needle]

    deal_cols = ", ".join(f"d.{c}" for c in _COLS)
    resp_cols = ", ".join(f"r.{c}" for c in _RESPONSE_COLS)
    base = (f"FROM deals d LEFT JOIN responses r "
            f"ON r.tenant_id=d.tenant_id AND r.site=d.site AND r.item_id=d.item_id "
            f"WHERE {' AND '.join(where)}")
    select = f"SELECT {deal_cols}, {resp_cols}, {state_expr} AS lead_state {base}"

    with _conn() as c:
        counted = c.execute(
            f"SELECT {state_expr} AS lead_state, COUNT(*) {base} GROUP BY 1",
            args,
        ).fetchall()
        counts = {s: 0 for s in LEAD_STATES}
        for value, n in counted:
            if value in counts:
                counts[value] = int(n)
        counts["all"] = sum(counts[s] for s in LEAD_STATES)

        if state in LEAD_STATES:
            # The CASE has to be repeated in the WHERE clause: neither engine
            # lets a WHERE reference a SELECT alias. Its parameters therefore
            # bind a second time, after every existing WHERE parameter.
            select_filtered = f"{select} AND {state_expr} = ?"
            rows_args = args + failed + [state]
            total = counts.get(state, 0)
        else:
            select_filtered = select
            rows_args = list(args)
            total = counts["all"]
        offset = (page - 1) * per_page
        rows = c.execute(
            f"{select_filtered} ORDER BY d.inquiry_at DESC, d.id DESC "
            f"LIMIT ? OFFSET ?",
            rows_args + [per_page, offset],
        ).fetchall()

    out = []
    for row in rows:
        deal = dict(zip(_COLS, row[:len(_COLS)]))
        resp_values = row[len(_COLS):len(_COLS) + len(_RESPONSE_COLS)]
        response = dict(zip(_RESPONSE_COLS, resp_values))
        out.append({
            "deal": deal,
            # A deal with no responder row yet has an all-None join; that is
            # "the agent hasn't looked", not an empty decision.
            "response": response if response.get("status") else None,
            "state": row[-1],
        })

    pages = max(1, (total + per_page - 1) // per_page)
    return {"rows": out, "counts": counts, "total": total,
            "page": page, "pages": pages, "per_page": per_page}


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
