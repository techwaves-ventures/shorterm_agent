"""Autopilot scheduling — when to check FurnishedFinder without being asked.

Owners don't want to remember to click "Check now"; a lead sitting unseen for a
day is a lead lost to whoever answered first. Autopilot runs the check on a
fixed daily schedule (default 09:00 and 16:00 local, inside business hours) and
lets the rest of the pipeline take it from there: new leads are drafted, and if
the owner has armed the first-reply step those drafts go out on their own.

Design notes:
  * Times are plain local `HH:MM` strings the owner can edit, not cron. Two
    slots a day is the shape of the problem; a cron expression would be a worse
    UI for the same result.
  * A slot fires at most once per day, tracked by `last_check_at`. Restarting
    the app or running several workers therefore can't double-check a tenant.
  * A missed slot (laptop asleep, worker down) fires late the same day rather
    than being skipped — better to check at 11:00 than not at all — but never
    rolls over to the next day.
"""
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config

log = logging.getLogger(__name__)


def tz_for(tenant_id: str):
    """The property's timezone, or the server's if the tenant hasn't set one.

    Every schedule (checks and the digest) is expressed in *property* local time
    — a host in DC wants their 6pm summary at 6pm Eastern regardless of where
    this process runs, and hosted deploys run in UTC.
    """
    name = str(config.get_settings(tenant_id).get("timezone") or "").strip()
    if not name:
        return None  # naive/server-local
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Unknown timezone %r for tenant %s; using server time", name, tenant_id)
        return None


def local_now(tenant_id: str, now: datetime | None = None) -> datetime:
    """`now` expressed in the tenant's property-local time, as a naive datetime.

    Naive on purpose: the stored `last_*_at` stamps are naive local strings, so
    keeping the comparison in one frame avoids mixing aware and naive values.
    """
    tz = tz_for(tenant_id)
    if tz is None:
        # No property timezone set: everything stays in the server's frame.
        return (now or datetime.now()).replace(tzinfo=None)
    if now is None:
        return datetime.now(tz).replace(tzinfo=None)
    # A naive `now` is server wall-clock (that's what datetime.now() gives the
    # callers); attach the system zone before converting so the arithmetic is
    # explicit rather than relying on astimezone's implicit assumption.
    aware = now if now.tzinfo else now.astimezone()
    return aware.astimezone(tz).replace(tzinfo=None)

# Autopilot never fires outside these hours even if the owner sets a silly time:
# a 3am browser login racks up FurnishedFinder security emails for no benefit.
BUSINESS_START = time(7, 0)
BUSINESS_END = time(21, 0)


def is_on(tenant_id: str) -> bool:
    return str(config.get_settings(tenant_id).get("autopilot")) in ("1", "true", "True")


def parse_times(raw: str | None) -> list[time]:
    """Parse "09:00,16:00" into times, dropping anything unparseable or unsociable."""
    out: list[time] = []
    for chunk in str(raw or config.DEFAULT_CHECK_TIMES).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            hh, _, mm = chunk.partition(":")
            t = time(int(hh), int(mm or 0))
        except (TypeError, ValueError):
            log.warning("Ignoring unparseable check time %r", chunk)
            continue
        if BUSINESS_START <= t <= BUSINESS_END:
            out.append(t)
        else:
            log.warning("Ignoring out-of-hours check time %s", t)
    return sorted(set(out))


def check_times(tenant_id: str) -> list[time]:
    return parse_times(config.get_settings(tenant_id).get("check_times"))


def due_slot(tenant_id: str, now: datetime | None = None) -> time | None:
    """The scheduled slot this tenant is currently owed, or None.

    Returns the latest slot that has already passed today and hasn't been
    covered by `last_check_at`. Comparing against the *slot* (not "N hours since
    last check") is what makes this idempotent: once a check runs, every slot
    at or before it is satisfied for the rest of the day.
    """
    # Enforced: a tenant on email ingestion is never scheduled for a scrape.
    # Scheduled browser checks are the highest-frequency automated access to
    # FurnishedFinder, and the whole point of the email path is not doing them.
    if config.ingest_mode(tenant_id) != config.INGEST_BROWSER:
        return None
    now = local_now(tenant_id, now)
    if not is_on(tenant_id):
        return None
    if not (BUSINESS_START <= now.time() <= BUSINESS_END):
        return None

    settings = config.get_settings(tenant_id)
    slots = parse_times(settings.get("check_times"))
    if not slots:
        return None

    passed = [s for s in slots if s <= now.time()]
    if not passed:
        return None
    latest = passed[-1]

    last_raw = str(settings.get("last_check_at") or "")
    if last_raw:
        try:
            last = datetime.fromisoformat(last_raw)
        except ValueError:
            last = None
        if last is not None:
            # Already checked today at or after this slot → nothing owed.
            if last.date() == now.date() and last.time() >= latest:
                return None
    return latest


def mark_checked(tenant_id: str, when: datetime | None = None) -> None:
    """Record that an automatic check ran, closing out the current slot.

    Stored in property-local time so it compares directly against the slots.
    """
    stamp = local_now(tenant_id, when).isoformat(timespec="seconds")
    config.save_settings(tenant_id, last_check_at=stamp)


# ---------------------------------------------------------------------------
# Daily digest
# ---------------------------------------------------------------------------


def digest_on(tenant_id: str) -> bool:
    return str(config.get_settings(tenant_id).get("digest_enabled")) in ("1", "true", "True")


def digest_time(tenant_id: str) -> time:
    raw = str(config.get_settings(tenant_id).get("digest_hour") or config.DEFAULT_DIGEST_HOUR)
    parsed = parse_times(raw)
    return parsed[0] if parsed else time(18, 0)


def digest_due(tenant_id: str, now: datetime | None = None) -> bool:
    """Whether this tenant is owed their end-of-day summary.

    Same once-per-day-per-slot rule as checks: sending twice is worse than
    sending late, so a digest already sent today at or after the hour wins.
    """
    if not digest_on(tenant_id):
        return False
    now = local_now(tenant_id, now)
    target = digest_time(tenant_id)
    if now.time() < target:
        return False
    last_raw = str(config.get_settings(tenant_id).get("last_digest_at") or "")
    if last_raw:
        try:
            last = datetime.fromisoformat(last_raw)
        except ValueError:
            last = None
        if last is not None and last.date() == now.date() and last.time() >= target:
            return False
    return True


def mark_digest_sent(tenant_id: str, when: datetime | None = None) -> None:
    config.save_settings(
        tenant_id, last_digest_at=local_now(tenant_id, when).isoformat(timespec="seconds")
    )


def digest_due_tenants(now: datetime | None = None) -> list[str]:
    import models

    out = []
    for tenant_id in models.all_tenant_ids():
        try:
            if digest_due(tenant_id, now):
                out.append(tenant_id)
        except Exception:
            log.exception("Could not evaluate digest schedule for %s", tenant_id)
    return out


def next_run(tenant_id: str, now: datetime | None = None) -> str:
    """Human-readable next scheduled check, for the dashboard."""
    now = local_now(tenant_id, now)
    slots = check_times(tenant_id)
    if not slots or not is_on(tenant_id):
        return "off"
    upcoming = [s for s in slots if s > now.time()]
    if upcoming:
        return f"today at {upcoming[0].strftime('%H:%M')}"
    return f"tomorrow at {slots[0].strftime('%H:%M')}"


def due_tenants(now: datetime | None = None) -> list[str]:
    """Every tenant currently owed an automatic check."""
    import models

    out = []
    for tenant_id in models.all_tenant_ids():
        try:
            if due_slot(tenant_id, now):
                out.append(tenant_id)
        except Exception:
            log.exception("Could not evaluate autopilot schedule for %s", tenant_id)
    return out
