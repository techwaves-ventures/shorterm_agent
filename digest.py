"""End-of-day summary email — the product's presence when nobody is logged in.

Most owners will not open a dashboard daily. The digest is what makes the agent's
work visible and what pulls them back in when something actually needs them, so
it leads with the two things that cost money if ignored: guests still waiting on
a reply, and messages that failed to send.

Sent once a day at the property's local hour (default 18:00 — see
scheduler.digest_due). Plain text on purpose: it renders everywhere, never trips
a spam filter on images, and is readable on a phone lock screen.

Guest names appear (it's the host's own inbox), but never credentials, never
another tenant's data, and never the full reply bodies — the digest is a prompt
to act, not a copy of the CRM.
"""
import logging
from datetime import datetime, timedelta

import config
import mailer
import outbox
import pipeline
import scheduler
import storage

log = logging.getLogger(__name__)

SITE = "furnishedfinder"


def _recipient(tenant_id: str) -> str:
    """Where the digest goes: the tenant's reply-from address, else their login."""
    settings = config.get_settings(tenant_id)
    to = (settings.get("from_email") or "").strip()
    if to:
        return to
    try:
        import models

        with models._conn() as c:
            row = c.execute(
                "SELECT email FROM users WHERE tenant_id=? ORDER BY id LIMIT 1",
                (int(tenant_id),),
            ).fetchone()
        return row[0] if row else ""
    except Exception:
        log.exception("Could not resolve digest recipient for tenant %s", tenant_id)
        return ""


def build(tenant_id: str, now: datetime | None = None) -> dict | None:
    """Assemble the digest. Returns None when there is genuinely nothing to say.

    Silence is a feature: an empty daily email trains people to ignore the
    channel, so a day with no leads, no pending approvals and no failures sends
    nothing at all.
    """
    local_now = scheduler.local_now(tenant_id, now)
    since = local_now - timedelta(hours=24)

    responses = storage.get_responses(tenant_id, SITE)
    deals = pipeline.all_deals(tenant_id, SITE)
    items = storage.all_items(tenant_id, SITE)

    def is_new(deal: dict) -> bool:
        created = deal.get("created_at") or ""
        return bool(created) and str(created) >= since.isoformat(timespec="seconds")

    new_deals = [d for d in deals if is_new(d)]
    waiting = pipeline.needs_action(deals, responses)
    arrivals = pipeline.arrivals(deals, within_days=7)
    metrics = pipeline.metrics(deals, responses)

    messages = outbox.for_tenant(
        tenant_id, SITE, (outbox.PENDING, outbox.SENT, outbox.FAILED)
    )
    sent_today = [
        m for m in messages
        if m["status"] == outbox.SENT and str(m.get("sent_at") or "") >= since.isoformat(timespec="seconds")
    ]
    failed = [m for m in messages if m["status"] == outbox.FAILED]
    pending = [m for m in messages if m["status"] == outbox.PENDING]

    if not any([new_deals, waiting, sent_today, failed, pending, arrivals]):
        return None

    host = (config.get_settings(tenant_id).get("host_name") or "").strip()
    lines: list[str] = []
    greeting = f"Hi {host}," if host else "Hi,"
    lines += [greeting, ""]

    # Lead with what the agent did unprompted — that's the value being paid for.
    if sent_today:
        lines.append(f"The agent sent {len(sent_today)} repl{'y' if len(sent_today) == 1 else 'ies'} today:")
        for m in sent_today[:8]:
            who = _guest_for(m["item_id"], deals, items)
            lines.append(f"  - {who} ({m.get('step_label') or 'reply'})")
        lines.append("")

    if new_deals:
        lines.append(f"{len(new_deals)} new inquir{'y' if len(new_deals) == 1 else 'ies'}:")
        for d in new_deals[:8]:
            bits = [d.get("guest_name") or "Guest"]
            if d.get("check_in"):
                bits.append(f"from {d['check_in']}")
            if d.get("nights"):
                bits.append(f"{d['nights']} nights")
            lines.append("  - " + ", ".join(bits))
        lines.append("")

    # Then what needs them — the part that costs a booking if skipped.
    if waiting:
        lines.append(f"** {len(waiting)} guest{'' if len(waiting) == 1 else 's'} still waiting on you **")
        for d in waiting[:8]:
            age = pipeline.humanize_age(d.get("inquiry_at"))
            lines.append(f"  - {d.get('guest_name') or 'Guest'} — waiting {age}")
        lines.append("")

    if pending:
        lines.append(f"{len(pending)} agent draft{'' if len(pending) == 1 else 's'} awaiting your approval.")
        lines.append("")

    if failed:
        lines.append(f"** {len(failed)} message{'' if len(failed) == 1 else 's'} FAILED to send **")
        for m in failed[:5]:
            who = _guest_for(m["item_id"], deals, items)
            lines.append(f"  - {who}: {m.get('error') or 'unknown error'}")
        lines.append("")

    if arrivals:
        lines.append(f"Arriving in the next 7 days: {len(arrivals)}")
        for d in arrivals[:6]:
            lines.append(f"  - {d.get('guest_name') or 'Guest'} on {d.get('check_in')}")
        lines.append("")

    lines += [
        "—",
        f"Open deals: {metrics['open_count']} · "
        f"Median first reply: {metrics['median_response_label']} · "
        f"Booked: {metrics['booked_count']}",
    ]

    subject = _subject(len(waiting), len(new_deals), len(failed))
    return {"subject": subject, "body": "\n".join(lines)}


def _subject(waiting: int, new: int, failed: int) -> str:
    """Front-load the action in the subject — it's often all they read."""
    if failed:
        return f"Shorterm: {failed} message{'' if failed == 1 else 's'} failed to send"
    if waiting:
        return f"Shorterm: {waiting} guest{'' if waiting == 1 else 's'} waiting on you"
    if new:
        return f"Shorterm: {new} new inquir{'y' if new == 1 else 'ies'} today"
    return "Shorterm: today's summary"


def _guest_for(item_id: str, deals: list[dict], items: dict) -> str:
    for d in deals:
        if d["item_id"] == item_id:
            return d.get("guest_name") or "Guest"
    item = items.get(item_id) or {}
    return item.get("traveler") or item.get("sender") or "Guest"


def send(tenant_id: str, now: datetime | None = None) -> bool:
    """Build and email the digest. Returns True if something was sent."""
    if not mailer.is_configured():
        log.info("Digest skipped for tenant %s — SMTP not configured", tenant_id)
        return False
    to = _recipient(tenant_id)
    if not to:
        log.warning("Digest skipped for tenant %s — no recipient address", tenant_id)
        return False
    content = build(tenant_id, now)
    if not content:
        log.info("Digest skipped for tenant %s — nothing to report", tenant_id)
        return False
    # The digest goes to the host's own inbox, so it needs no reply-to
    # indirection — send it plainly from our verified sender.
    mailer.send_email(to, content["subject"], content["body"])
    return True


def run_due(now: datetime | None = None) -> int:
    """Send the digest to every tenant whose local digest hour has passed.

    The send is marked *before* dispatch so a transient SMTP failure can't cause
    the same summary to be re-sent on the next pass a minute later.
    """
    sent = 0
    for tenant_id in scheduler.digest_due_tenants(now):
        try:
            scheduler.mark_digest_sent(tenant_id, now)
            if send(tenant_id, now):
                sent += 1
                log.info("Digest sent to tenant %s", tenant_id)
        except Exception:
            log.exception("Digest failed for tenant %s", tenant_id)
    return sent
