"""Flask dashboard: view recent FurnishedFinder leads/messages and trigger a
live scrape (with in-browser OTP entry).

Multi-tenant: every request is scoped to the logged-in user's tenant. Scraping
and sending are gated to the *operator* tenant until each tenant gets its own
FurnishedFinder login (Phase 3); other tenants see an empty dashboard.

Run:
    .venv/bin/python dashboard.py
    open http://localhost:5050
"""
import json
import os
from datetime import timedelta
from functools import wraps

from dotenv import load_dotenv

load_dotenv()

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_wtf.csrf import CSRFProtect
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)

import automation
import billing
import check_leads
import config
import crypto
import ff_account
import inbound
import jobs
import models
import outbox
import pipeline
import responder
import runner
import scheduler
import sequences
import storage
import waitlist

SITE = "furnishedfinder"
LIMIT = 20

# A short list beats the ~600 IANA zones in a dropdown. US-heavy because that's
# where FurnishedFinder operates; the DB column accepts any valid zone name.
COMMON_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
    "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu",
    "America/Toronto", "America/Vancouver", "Europe/London", "Europe/Dublin",
    "Europe/Lisbon", "Europe/Madrid", "Europe/Paris", "Europe/Berlin",
    "Europe/Amsterdam", "Europe/Warsaw", "Asia/Jerusalem", "Asia/Dubai",
    "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney",
]

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or ""
if not app.secret_key:
    # Sessions can't be signed without a key. Fail loud rather than ship an
    # insecure default — set SECRET_KEY in .env (see .env.example).
    raise RuntimeError("SECRET_KEY not set in .env — required for login sessions.")

# --- Session hardening -----------------------------------------------------
# Session cookies carry a logged-in host's identity, so they never go to JS and
# never ride a cross-site request. `Secure` is on unless explicitly disabled for
# local http development (set INSECURE_COOKIES=1 only on 127.0.0.1).
_insecure_cookies = os.getenv("INSECURE_COOKIES", "").strip().lower() in ("1", "true", "yes")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not _insecure_cookies,
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    # Reject oversized request bodies before they're parsed.
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)

# --- CSRF ------------------------------------------------------------------
# Every state-changing route here is a cookie-authenticated POST, so without
# this any page a logged-in host visits could make their browser send replies to
# guests, disconnect their FurnishedFinder account, or change their settings.
# Stripe's webhook is the one exempt endpoint: it's server-to-server and is
# authenticated by its own signature, not by a session.
csrf = CSRFProtect(app)
csrf.exempt(billing.webhook)


def public_signup_enabled() -> bool:
    return os.getenv("PUBLIC_SIGNUP_ENABLED", "").strip().lower() in ("1", "true", "yes")

def _bootstrap_on_boot() -> None:
    """Provision the operator (and optionally seed demo data) at startup.

    Wrapped in try/except so a transient DB outage at a serverless cold start
    doesn't crash the whole function — the app still boots and /healthz reports
    `db: false` (503) until the DB recovers, instead of failing to import.
    Provisioning is idempotent and can also be run explicitly via
    `python manage.py init`.
    """
    try:
        models.ensure_operator()
        # Opt-in one-time demo seed for hosted instances (e.g. Vercel), where
        # running a CLI is awkward. Set SEED_DEMO_ON_BOOT=1. Idempotent: only
        # seeds when the demo tenant doesn't exist yet.
        if os.getenv("SEED_DEMO_ON_BOOT", "").strip().lower() in ("1", "true", "yes"):
            import seed_demo

            if not models.get_user_by_email(seed_demo.DEMO_EMAIL):
                seed_demo.seed_demo()
    except Exception:
        app.logger.exception("Boot bootstrap failed (DB unreachable?); continuing.")


_bootstrap_on_boot()


def _start_background_agents() -> None:
    """Start the autopilot scheduler on hosts that can drive a browser.

    On serverless (no Playwright) or when the worker queue is forced, worker.py
    owns this instead — starting a scheduler here would fire checks that can
    never run.
    """
    try:
        if check_leads.playwright_available() and not _use_worker_queue():
            automation.start_scheduler(SITE)
    except Exception:
        app.logger.exception("Could not start the autopilot scheduler")


login_manager = LoginManager(app)
login_manager.login_view = "login"

# Stripe billing routes (demo-safe when no keys are set — see billing.py).
app.register_blueprint(billing.billing_bp)


@login_manager.user_loader
def load_user(user_id):
    return models.get_user_by_id(user_id)


def _can_scrape() -> bool:
    """A tenant may attempt a scrape/verification once they've linked an FF email
    (even before it's verified — that first Check now IS the verification); the
    operator is grandfathered in via the FF_USERNAME env."""
    return current_user.is_operator or ff_account.has_account(current_user.tenant_id)


def _use_worker_queue() -> bool:
    """Whether scrape/OTP routes should use the DB worker queue.

    Vercel uses this path because Playwright is unavailable there. A VM-local UI
    can opt into the same path with FORCE_WORKER_QUEUE=1 so browser automation
    stays in the durable worker service instead of the web process.
    """
    return os.getenv("FORCE_WORKER_QUEUE", "").strip().lower() in ("1", "true", "yes")


def _can_deliver_in_process() -> bool:
    """Whether this process can drive the browser to deliver queued messages.

    On serverless (no Playwright) or when the worker queue is forced, delivery
    belongs to worker.py instead — starting a drainer here would spin uselessly.
    """
    return check_leads.playwright_available() and not _use_worker_queue()


def _live_state(tenant_id: str) -> dict:
    """Runner-compatible run state for the UI.

    On a host with Playwright (local/worker-host dashboard) the scrape runs
    in-process, so the live state comes from the runner. On serverless (Vercel,
    no Playwright) scrapes are worker-backed via the shared DB, so the state is
    projected from the tenant's latest job. Same shape either way."""
    if check_leads.playwright_available() and not _use_worker_queue():
        return runner.get_state(tenant_id)
    return jobs.public_state(tenant_id)


def scrape_allowed(fn):
    """Gate scrape/send routes to tenants who've connected their FF account."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not _can_scrape():
            abort(403, "Connect your FurnishedFinder account first.")
        return fn(*args, **kwargs)

    return wrapper


def _recent(tenant_id: str):
    responses = storage.get_responses(tenant_id, SITE)
    leads = storage.get_recent(tenant_id, SITE, "lead", LIMIT)
    messages = storage.get_recent(tenant_id, SITE, "message", LIMIT)
    for it in leads:
        it["kind"] = "lead"
        it["response"] = responses.get(it.get("id"))
    for it in messages:
        it["kind"] = "message"
        it["response"] = responses.get(it.get("id"))
    return {"leads": leads, "messages": messages}


def _item_by_id(tenant_id: str, item_id: str) -> dict | None:
    """Find a lead or message by id (within this tenant), tagging its kind."""
    return storage.get_item(tenant_id, SITE, item_id)


# ---------------------------------------------------------------------------
# Command-center view model (deals + lifecycle + agent queue)
# ---------------------------------------------------------------------------


def _board(tenant_id: str) -> dict:
    """Everything the dashboard renders, assembled in a fixed number of queries.

    A "card" is one deal joined to its scraped payload, the agent's fit
    decision, and any message currently awaiting approval — the unit the whole
    UI is built from, replacing the old separate leads/messages lists.
    """
    responses = storage.get_responses(tenant_id, SITE)
    items = storage.all_items(tenant_id, SITE)
    # Items scraped before the pipeline existed get a deal on first view, so an
    # existing install picks up the lifecycle without a migration step.
    pipeline.backfill(tenant_id, SITE, items, responses, config.get_units(tenant_id))
    deals = pipeline.all_deals(tenant_id, SITE)
    pending = {
        m["item_id"]: m
        for m in outbox.for_tenant(tenant_id, SITE, (outbox.PENDING,))
    }
    send_states = outbox.latest_by_item(tenant_id, SITE)
    # Self-heal on view: requeue sends stranded by a crashed process, and make
    # sure something is draining if messages are waiting (e.g. after a restart).
    if _can_deliver_in_process():
        outbox.reclaim_stuck_sending()
        if outbox.queued_tenants():
            automation.start_drainer(SITE)

    def card(deal: dict) -> dict:
        item = items.get(deal["item_id"], {})
        return {
            "deal": deal,
            "item": item,
            "response": responses.get(deal["item_id"]),
            "pending": pending.get(deal["item_id"]),
            "send": send_states.get(deal["item_id"]),
            "age": pipeline.humanize_age(deal.get("inquiry_at")),
            "age_hours": pipeline.age_hours(deal.get("inquiry_at")),
            "stage_label": pipeline.STAGE_LABELS.get(deal.get("stage"), deal.get("stage")),
            "title": item.get("title", ""),
            "id": deal["item_id"],
        }

    by_id = {d["item_id"]: d for d in deals}
    auto_settings = automation.settings_for(tenant_id)
    needs = [card(d) for d in pipeline.needs_action(deals, responses)]
    all_cards = sorted(
        (card(d) for d in deals),
        key=lambda c: c["deal"].get("inquiry_at") or "",
        reverse=True,
    )
    return {
        "metrics": pipeline.metrics(deals, responses),
        "needs_action": needs,
        "reviewable": [card(d) for d in pipeline.reviewable(deals, responses)],
        "all": all_cards,
        "scheduled": [card(d) for d in pipeline.scheduled(deals)][:12],
        "arrivals": [card(d) for d in pipeline.arrivals(deals)],
        "awaiting_approval": [
            {**card(by_id[m["item_id"]]), "pending": m}
            for m in outbox.for_tenant(tenant_id, SITE, (outbox.PENDING,))
            if m["item_id"] in by_id
        ],
        "outbox_counts": outbox.counts(tenant_id, SITE),
        # `steps` is a set internally (membership tests); listify so the board
        # stays JSON-serializable for /api/board.
        "automation": {**auto_settings, "steps": sorted(auto_settings["steps"])},
        "stale_count": max(0, len(deals) - len(needs)),
    }


def _deal_or_404(tenant_id: str, item_id: str) -> dict:
    deal = pipeline.get(tenant_id, SITE, item_id)
    if not deal:
        abort(404, "No such deal.")
    return deal


# ---------------------------------------------------------------------------
# Public pages (landing, health, pilot waitlist)
# ---------------------------------------------------------------------------


@app.route("/")
def landing():
    """Public marketing landing page. Authenticated users go to their dashboard."""
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    return render_template("landing.html")


@app.route("/healthz")
def healthz():
    """Liveness/readiness probe for the hosting platform. Checks DB reachability."""
    db_ok = True
    try:
        models.get_user_by_id(0)  # cheap query; opens + initializes the DB
    except Exception:
        db_ok = False
    payload = {
        "status": "ok" if db_ok else "degraded",
        "service": "shorterm-agent",
        "db": db_ok,
        "crypto_configured": crypto.available(),
        "billing_mode": "demo" if billing.demo_mode() else "live",
    }
    return jsonify(payload), (200 if db_ok else 503)


@app.route("/inbound/email", methods=["POST"])
@csrf.exempt
def inbound_email():
    """Receive a forwarded FurnishedFinder notification from the mail provider.

    Server-to-server, so it's authenticated by the provider secret rather than a
    session — hence the CSRF exemption (same rationale as the Stripe webhook).

    Always answers 202 regardless of outcome: a flat response means a prober
    can't use this endpoint to discover which tenants or addresses exist, and
    mail providers retry on non-2xx, which would replay a message we already
    deliberately rejected.
    """
    supplied = (
        request.headers.get("X-Inbound-Secret")
        or request.args.get("secret")
        or (request.form.get("secret") if request.form else "")
        or ""
    )
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        tenant_id, item = inbound.accept(
            payload, supplied, raw_size=request.content_length or 0
        )
    except inbound.Rejected as e:
        app.logger.warning("Inbound email rejected: %s", e)
        return ("", 202)
    except Exception:
        app.logger.exception("Inbound email handling failed")
        return ("", 202)

    try:
        is_new = inbound.store(tenant_id, item, SITE)
        app.logger.info(
            "Inbound %s for tenant %s (%s)",
            item.get("kind"), tenant_id, "new" if is_new else "duplicate",
        )
        if is_new and os.getenv("ANTHROPIC_API_KEY"):
            # Draft immediately — answering fast is the entire point of taking
            # the email path, so this shouldn't wait for a scheduled pass.
            runner.draft_ingested(tenant_id, SITE, item)
    except Exception:
        app.logger.exception("Could not store inbound item")
    return ("", 202)


@app.route("/pilot", methods=["POST"])
def pilot():
    """Public pilot-access request from the landing page."""
    try:
        waitlist.add(
            request.form.get("email", ""),
            request.form.get("market", ""),
            request.form.get("units", ""),
        )
        flash("Thanks — you're on the pilot list. We'll be in touch shortly.")
    except ValueError as e:
        flash(str(e))
    return redirect(url_for("landing") + "#pilot")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        # Throttle before doing any password work, so a locked account can't be
        # used as a timing oracle or to burn CPU on hash comparisons.
        locked = models.lockout_remaining(email)
        if locked:
            flash(f"Too many failed attempts. Try again in {max(1, locked // 60)} minute(s).")
            return render_template(
                "auth.html", title="Log in", subtitle="Sign in to your dashboard.",
                action=url_for("login"),
                alt_text='<a href="%s">Forgot your password?</a>' % url_for("forgot_password"),
            )
        user = models.verify_password(email, password)
        if user:
            models.clear_login_failures(email)
            login_user(user)
            return redirect(url_for("index"))
        models.record_login_failure(email)
        flash("Invalid email or password.")
    return render_template(
        "auth.html",
        title="Log in",
        subtitle="Sign in to your dashboard.",
        action=url_for("login"),
        alt_text='<a href="%s">Forgot your password?</a>' % url_for("forgot_password"),
    )


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if not public_signup_enabled():
        flash("Public signup is closed for now. Request access and we'll set you up.")
        return redirect(url_for("landing") + "#pilot")
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        try:
            user = models.create_user(email, password)
        except ValueError as e:
            flash(str(e))
        else:
            login_user(user)
            return redirect(url_for("index"))
    return render_template(
        "auth.html",
        title="Sign up",
        subtitle="Create your STR Leads account.",
        action=url_for("signup"),
        alt_text='Already have an account? <a href="%s">Log in</a>' % url_for("login"),
    )


@app.route("/forgot", methods=["GET", "POST"])
def forgot_password():
    """Request a password-reset link.

    Always reports success, whether or not the address exists — otherwise this
    page becomes an oracle for which emails have accounts.
    """
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        token = models.make_reset_token(email, app.secret_key)
        if token:
            link = url_for("reset_password", token=token, _external=True)
            try:
                if mailer.is_configured():
                    mailer.send_email(
                        email,
                        "Reset your Shorterm password",
                        "Someone asked to reset the password for this Shorterm account.\n\n"
                        f"Use this link within the hour:\n{link}\n\n"
                        "If it wasn't you, you can ignore this email — nothing has changed.",
                    )
                else:
                    # No relay configured (local/pilot): log it so the operator
                    # can hand the link over. Never shown in the response.
                    app.logger.warning("Password reset link for %s: %s", email, link)
            except Exception:
                app.logger.exception("Could not send password reset email")
        flash("If that email has an account, a reset link is on its way.")
        return redirect(url_for("login"))
    return render_template(
        "auth.html",
        title="Reset your password",
        subtitle="We'll email you a link to set a new one.",
        action=url_for("forgot_password"),
        password_field=False,
        alt_text='Remembered it? <a href="%s">Log in</a>' % url_for("login"),
    )


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    """Set a new password from an emailed link."""
    email = models.verify_reset_token(token, app.secret_key)
    if not email:
        flash("That reset link is invalid or has expired — request a new one.")
        return redirect(url_for("forgot_password"))
    if request.method == "POST":
        password = request.form.get("password") or ""
        try:
            models.set_password(email, password)
        except ValueError as e:
            flash(str(e))
            return redirect(url_for("reset_password", token=token))
        models.clear_login_failures(email)
        flash("Password updated — you can log in now.")
        return redirect(url_for("login"))
    return render_template(
        "auth.html",
        title="Choose a new password",
        subtitle=f"Setting a new password for {email}.",
        action=url_for("reset_password", token=token),
        email_field=False,
        password_hint=f"At least {models.MIN_PASSWORD_LENGTH} characters.",
        alt_text="",
    )


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@app.route("/dashboard")
@login_required
def index():
    # First-run tenants are guided through onboarding before the dashboard.
    if not current_user.is_operator and not config.is_onboarded(current_user.tenant_id):
        return redirect(url_for("onboarding"))
    tenant_id = current_user.tenant_id
    return render_template(
        "dashboard.html",
        nav_active="dashboard",
        account=current_user.email,
        is_operator=current_user.is_operator,
        can_scrape=_can_scrape(),
        ff_status=ff_account.status(tenant_id),
        crypto_ready=crypto.available(),
        board=_board(tenant_id),
        state=_live_state(tenant_id),
        has_api_key=bool(os.getenv("ANTHROPIC_API_KEY")),
        billing_label=billing.status_label(billing.get_subscription(tenant_id)),
    )


@app.route("/api/board")
@login_required
def api_board():
    """Live view model for the dashboard's poll-refresh."""
    return jsonify(_board(current_user.tenant_id))


# ---------------------------------------------------------------------------
# Deal lifecycle actions
# ---------------------------------------------------------------------------


@app.route("/deal/<item_id>/booked", methods=["POST"])
@login_required
def deal_booked(item_id):
    """Mark a deal booked — this is what arms the pre-arrival agent.

    Booking happens off-platform (a lease, a call), so it can't be scraped; one
    explicit click is both the honest signal and the trigger for the whole
    post-booking sequence.
    """
    tenant_id = current_user.tenant_id
    _deal_or_404(tenant_id, item_id)
    check_in, check_out = _form("check_in", "check_out")
    automation.start_prearrival(
        tenant_id, SITE, item_id, check_in or None, check_out or None
    )
    return jsonify({"ok": True, "deal": pipeline.get(tenant_id, SITE, item_id)})


@app.route("/deal/<item_id>/lost", methods=["POST"])
@login_required
def deal_lost(item_id):
    tenant_id = current_user.tenant_id
    _deal_or_404(tenant_id, item_id)
    pipeline.mark_lost(tenant_id, SITE, item_id)
    return jsonify({"ok": True})


@app.route("/deal/<item_id>/stage", methods=["POST"])
@login_required
def deal_stage(item_id):
    """Set a deal's stage directly (pipeline drag/drop, manual correction)."""
    tenant_id = current_user.tenant_id
    _deal_or_404(tenant_id, item_id)
    (stage,) = _form("stage")
    if stage not in pipeline.STAGE_LABELS:
        return jsonify({"ok": False, "error": "unknown stage"}), 400
    pipeline.update(tenant_id, SITE, item_id, stage=stage)
    automation.reschedule(tenant_id, SITE, item_id)
    return jsonify({"ok": True, "deal": pipeline.get(tenant_id, SITE, item_id)})


# ---------------------------------------------------------------------------
# Agent outbox (approve / decline / run)
# ---------------------------------------------------------------------------


def _own_message_or_404(tenant_id: str, msg_id: int) -> dict:
    """Load an outbox row, refusing anything outside the caller's tenant."""
    msg = outbox.get(msg_id)
    if not msg or str(msg["tenant_id"]) != str(tenant_id):
        abort(404, "No such message.")
    return msg


@app.route("/outbox/<int:msg_id>/approve", methods=["POST"])
@login_required
def outbox_approve(msg_id):
    """Approve (optionally edited) agent copy and release it to the send queue."""
    tenant_id = current_user.tenant_id
    _own_message_or_404(tenant_id, msg_id)
    (text,) = _form("text")
    outbox.approve(msg_id, (text or "").strip() or None)
    automation.start_drainer(SITE)  # deliver in the background; don't block the click
    return jsonify({"ok": True, "counts": outbox.counts(tenant_id, SITE)})


@app.route("/outbox/<int:msg_id>/cancel", methods=["POST"])
@login_required
def outbox_cancel(msg_id):
    tenant_id = current_user.tenant_id
    _own_message_or_404(tenant_id, msg_id)
    outbox.cancel(msg_id)
    return jsonify({"ok": True, "counts": outbox.counts(tenant_id, SITE)})


@app.route("/automation/run", methods=["POST"])
@login_required
def automation_run():
    """Draft every lifecycle step that has come due for this tenant."""
    tenant_id = current_user.tenant_id
    if not os.getenv("ANTHROPIC_API_KEY"):
        return jsonify({"ok": False, "error": "ANTHROPIC_API_KEY not set"}), 400
    summary = automation.run_due(tenant_id, SITE)
    return jsonify({"ok": True, "summary": summary, "board": _board(tenant_id)})


# ---------------------------------------------------------------------------
# Automations settings
# ---------------------------------------------------------------------------


@app.route("/automations", methods=["GET", "POST"])
@login_required
def automations():
    tenant_id = current_user.tenant_id
    if request.method == "POST":
        enabled = "1" if request.form.get("automation_enabled") else "0"
        chosen = request.form.getlist("auto_steps")
        config.save_settings(
            tenant_id, automation_enabled=enabled, auto_steps=",".join(chosen)
        )
        flash("Automation settings saved.")
        return redirect(url_for("automations"))

    current = automation.settings_for(tenant_id)
    settings = config.get_settings(tenant_id)
    return render_template(
        "automations.html",
        nav_active="automations",
        account=current_user.email,
        sequences=[sequences.SEQUENCES[s] for s in (sequences.PRESALE, sequences.PREARRIVAL)],
        enabled=current["enabled"],
        auto_steps=current["steps"],
        autopilot=scheduler.is_on(tenant_id),
        check_times=settings.get("check_times") or config.DEFAULT_CHECK_TIMES,
        next_run=scheduler.next_run(tenant_id),
        counts=outbox.counts(tenant_id, SITE),
        has_api_key=bool(os.getenv("ANTHROPIC_API_KEY")),
    )


@app.route("/autopilot", methods=["POST"])
@login_required
@scrape_allowed
def autopilot_toggle():
    """Turn autopilot on/off. This is the one switch that grants real autonomy:
    scheduled checks plus an immediate reply to good-fit leads."""
    tenant_id = current_user.tenant_id
    on = bool(request.form.get("autopilot"))
    times = (request.form.get("check_times") or "").strip() or config.DEFAULT_CHECK_TIMES
    # Round-trip through the parser so an unusable schedule can't be saved.
    parsed = scheduler.parse_times(times)
    if on and not parsed:
        flash("Those check times couldn't be read — use 24-hour times like 09:00,16:00.")
        return redirect(url_for("automations"))
    config.save_settings(
        tenant_id,
        autopilot="1" if on else "0",
        check_times=",".join(t.strftime("%H:%M") for t in parsed) or config.DEFAULT_CHECK_TIMES,
    )
    if on:
        automation.start_scheduler(SITE)
        flash(f"Autopilot is on — checking {scheduler.next_run(tenant_id)} "
              "and replying to good-fit leads automatically.")
    else:
        flash("Autopilot is off. Checks and replies are manual again.")
    return redirect(url_for("automations"))


@app.route("/api/data")
@login_required
def api_data():
    return jsonify(_recent(current_user.tenant_id))


@app.route("/api/status")
@login_required
def api_status():
    state = _live_state(current_user.tenant_id)
    state["ff_status"] = ff_account.status(current_user.tenant_id)
    return jsonify(state)


@app.route("/refresh", methods=["POST"])
@login_required
@scrape_allowed
def refresh():
    tenant_id = current_user.tenant_id
    if check_leads.playwright_available() and not _use_worker_queue():
        # Browser is available here: run the scrape in-process (local/worker host).
        return jsonify(runner.start_scrape(tenant_id))
    # Serverless (Vercel): can't run Playwright in-process. Enqueue a job for the
    # off-Vercel worker that shares this DB, and report the worker-backed state.
    jobs.enqueue(tenant_id)
    return jsonify(_live_state(tenant_id))


@app.route("/otp", methods=["POST"])
@login_required
@scrape_allowed
def otp():
    tenant_id = current_user.tenant_id
    code = request.form.get("code", "") or (request.json or {}).get("code", "")
    if check_leads.playwright_available() and not _use_worker_queue():
        ok = runner.submit_otp(tenant_id, code)
    else:
        # Route the code to the tenant's active worker job via the shared DB.
        ok = jobs.submit_otp(tenant_id, code)
    return jsonify({"ok": ok, "state": _live_state(tenant_id)})


def _form(*keys):
    src = request.form if request.form else (request.json or {})
    return [src.get(k, "") for k in keys]


@app.route("/responder/draft", methods=["POST"])
@login_required
def responder_draft():
    """(Re-)draft a single lead or message on demand."""
    tenant_id = current_user.tenant_id
    (item_id,) = _form("item_id")
    item = _item_by_id(tenant_id, item_id)
    if not item:
        return jsonify({"ok": False, "error": "item not found"}), 404
    try:
        d = responder.evaluate_lead(item, tenant_id)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    storage.save_response(
        tenant_id, SITE, item["kind"], item_id,
        status="draft" if d.get("fit") else "skipped",
        unit_id=d.get("unit_id"), reason=d.get("reason"),
        draft=d.get("draft"), confidence=d.get("confidence"),
        tenant_email=d.get("tenant_email"),
    )
    return jsonify(
        {"ok": True, "response": storage.get_responses(tenant_id, SITE).get(item_id)}
    )


@app.route("/responder/send", methods=["POST"])
@login_required
@scrape_allowed
def responder_send():
    """One-click approve → queue the (possibly edited) draft for delivery.

    Returns as soon as the message is durably queued. Delivery drives a real
    browser and takes tens of seconds, so it happens on a background drainer —
    the user gets their UI back immediately and the card tracks the outcome via
    /api/send-states (including a failure, which also fires a notification).
    """
    tenant_id = current_user.tenant_id
    item_id, text = _form("item_id", "text")
    item = _item_by_id(tenant_id, item_id)
    if not item:
        return jsonify({"ok": False, "error": "item not found"}), 404
    if not (text or "").strip():
        return jsonify({"ok": False, "error": "empty reply"}), 400
    msg = automation.enqueue_send(tenant_id, SITE, item_id, text.strip())
    return jsonify({
        "ok": True,
        "queued": True,
        "message_id": (msg or {}).get("id"),
        "status": (msg or {}).get("status"),
    })


@app.route("/api/send-states")
@login_required
def api_send_states():
    """Per-item delivery state, polled by the cards after a send is queued."""
    tenant_id = current_user.tenant_id
    states = {}
    for item_id, msg in outbox.latest_by_item(tenant_id, SITE).items():
        states[item_id] = {
            "status": msg["status"],
            "label": outbox.STATUS_LABELS.get(msg["status"], msg["status"]),
            "error": msg.get("error"),
            "step": msg.get("step_label"),
            "in_flight": msg["status"] in outbox.IN_FLIGHT,
        }
    return jsonify(states)


@app.route("/outbox/<int:msg_id>/retry", methods=["POST"])
@login_required
def outbox_retry(msg_id):
    """Re-queue a failed send (the body is kept, so nothing is retyped)."""
    tenant_id = current_user.tenant_id
    msg = _own_message_or_404(tenant_id, msg_id)
    if msg["status"] != outbox.FAILED:
        return jsonify({"ok": False, "error": "only failed messages can be retried"}), 400
    outbox.set_status(msg_id, outbox.QUEUED, error="")
    automation.start_drainer(SITE)
    return jsonify({"ok": True})


@app.route("/responder/dismiss", methods=["POST"])
@login_required
def responder_dismiss():
    (item_id,) = _form("item_id")
    storage.update_response(current_user.tenant_id, SITE, item_id, status="dismissed")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Connect a FurnishedFinder account
# ---------------------------------------------------------------------------


@app.route("/connect", methods=["POST"])
@login_required
def connect():
    """Store the tenant's FF email (encrypted) + their consent to automated
    access. FF login is passwordless (email + code/magic link), so only the
    email is kept."""
    if not crypto.available():
        flash("Account connection isn't available yet (encryption not configured).")
        return redirect(url_for("index"))
    ff_email = (request.form.get("ff_email") or "").strip()
    consent = request.form.get("consent")
    if not ff_email:
        flash("Enter your FurnishedFinder email.")
        return redirect(url_for("index"))
    if not consent:
        flash("Please confirm you authorize automated access to your FurnishedFinder account.")
        return redirect(url_for("index"))
    try:
        ff_account.connect(current_user.tenant_id, ff_email)
    except (ValueError, RuntimeError) as e:
        flash(str(e))
        return redirect(url_for("index"))
    flash("FurnishedFinder email saved. It's not verified yet — click “Check now” to "
          "log in with the code or magic link FurnishedFinder emails you.")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Onboarding (guided first run)
# ---------------------------------------------------------------------------


def _unit_from_onboarding(form) -> list[dict] | None:
    """Build a one-unit catalog from the onboarding form, or None if left blank."""
    name = (form.get("unit_name") or "").strip()
    area = (form.get("unit_area") or "").strip()
    if not name and not area:
        return None
    unit: dict = {"id": "unit-1", "name": name or "Unit 1"}
    if area:
        unit["area"] = area
    for field, key in (("unit_price", "monthly_price"),
                       ("unit_occupancy", "max_occupancy"),
                       ("unit_min_nights", "min_nights")):
        val = (form.get(field) or "").strip()
        if val.isdigit():
            unit[key] = int(val)
    pets = form.get("unit_pets", "")
    if pets == "yes":
        unit["pets_allowed"] = True
    elif pets == "no":
        unit["pets_allowed"] = False
    notes = (form.get("unit_notes") or "").strip()
    if notes:
        unit["notes"] = notes
    return [unit]


@app.route("/onboarding", methods=["GET", "POST"])
@login_required
def onboarding():
    tenant_id = current_user.tenant_id
    current = config.get_settings(tenant_id)

    # "Skip for now" (a GET link) — mark done and go to the dashboard.
    if request.method == "GET" and request.args.get("skip"):
        config.mark_onboarded(tenant_id)
        flash("You can finish setup anytime in Settings.")
        return redirect(url_for("index"))

    if request.method == "POST":
        units = _unit_from_onboarding(request.form)
        units_json = json.dumps(units) if units else current["units_json"]
        config.save_settings(
            tenant_id,
            host_name=(request.form.get("host_name") or "").strip(),
            from_email=(request.form.get("from_email") or "").strip(),
            template=request.form.get("template") or current["template"],
            units_json=units_json,
        )
        # Optional FurnishedFinder connection (email + explicit consent).
        ff_email = (request.form.get("ff_email") or "").strip()
        if ff_email and request.form.get("ff_consent"):
            if not crypto.available():
                flash("FurnishedFinder not connected — encryption isn't configured yet.")
            else:
                try:
                    ff_account.connect(tenant_id, ff_email)
                except (ValueError, RuntimeError) as e:
                    flash(f"FurnishedFinder not connected: {e}")
        config.mark_onboarded(tenant_id)
        if ff_email and request.form.get("ff_consent") and crypto.available():
            flash("You're set up. Your FurnishedFinder email is saved but not verified yet — "
                  "click “Check now” to log in with the code or magic link they email you.")
        else:
            flash("You're all set. Connect FurnishedFinder to start fetching leads.")
        return redirect(url_for("index"))

    f = {
        "host_name": current["host_name"],
        "from_email": current["from_email"],
        "template": current["template"],
        "unit_name": "", "unit_area": "", "unit_price": "", "unit_occupancy": "",
        "unit_min_nights": "", "unit_pets": "", "unit_notes": "", "ff_email": "",
    }
    return render_template(
        "onboarding.html",
        account=current_user.email,
        crypto_ready=crypto.available(),
        f=f,
    )


@app.route("/disconnect", methods=["POST"])
@login_required
def disconnect():
    """Remove the tenant's connected FurnishedFinder account."""
    ff_account.disconnect(current_user.tenant_id)
    jobs.cancel_active(current_user.tenant_id)  # abandon any in-flight worker job
    flash("FurnishedFinder account disconnected.")
    return redirect(url_for("settings"))


# ---------------------------------------------------------------------------
# Per-tenant settings (units, template, identity)
# ---------------------------------------------------------------------------


def _settings_context(tenant_id: str, settings: dict, units: list[dict] | None = None) -> dict:
    """Shared template context for the settings page."""
    return {
        "nav_active": "settings",
        "account": current_user.email,
        "settings": settings,
        # Photos are joined on for display only — they're never saved back into
        # the catalog, so a re-scrape always wins and nothing goes stale.
        "units": units if units is not None else config.units_with_images(tenant_id, SITE),
        # Properties visible in this tenant's leads that aren't in the catalog
        # yet — offered as a one-click import instead of manual data entry.
        "discovered": config.discover_units(tenant_id, SITE),
        # Facts a scraped listing could fill in on units that lack them.
        "enrichments": config.suggested_enrichments(tenant_id, SITE),
        "channels": [c.strip() for c in (settings.get("reply_channels") or "").split(",") if c.strip()],
        "ff_status": ff_account.status(tenant_id),
        "crypto_ready": crypto.available(),
        "billing_label": billing.status_label(billing.get_subscription(tenant_id)),
        "timezones": COMMON_TIMEZONES,
        "ingest_mode": config.ingest_mode(tenant_id),
        "inbound_address": inbound.address_for(tenant_id),
        "inbound_ready": inbound.configured(),
    }


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    tenant_id = current_user.tenant_id
    if request.method == "POST":
        units = config.units_from_form(request.form)
        channels = ",".join(request.form.getlist("reply_channels")) or "platform"
        config.save_settings(
            tenant_id,
            host_name=request.form.get("host_name", "").strip(),
            units_json=json.dumps(units, indent=2),
            template=request.form.get("template", ""),
            from_email=request.form.get("from_email", "").strip(),
            reply_channels=channels,
            notify_webhook=request.form.get("notify_webhook", "").strip(),
            ingest_mode=(config.INGEST_BROWSER
                         if request.form.get("ingest_mode") == config.INGEST_BROWSER
                         else config.INGEST_EMAIL),
            digest_enabled="1" if request.form.get("digest_enabled") else "0",
            digest_hour=(request.form.get("digest_hour") or "").strip() or config.DEFAULT_DIGEST_HOUR,
            timezone=(request.form.get("timezone") or "").strip(),
        )
        flash(f"Saved — {len(units)} propert{'y' if len(units) == 1 else 'ies'} in your catalog.")
        return redirect(url_for("settings"))
    return render_template(
        "settings.html",
        **_settings_context(tenant_id, config.get_settings(tenant_id)),
    )


@app.route("/settings/enrich", methods=["POST"])
@login_required
def enrich_units():
    """Fill in missing unit facts from the tenant's scraped FurnishedFinder listing.

    Additive only, and only for the boxes the host ticked — the agent quotes
    these numbers to real guests, so nothing is copied without them seeing the
    exact value first.
    """
    tenant_id = current_user.tenant_id
    applied = config.apply_enrichments(
        tenant_id, request.form.getlist("apply"), SITE
    )
    if applied:
        flash(f"Filled in {applied} detail{'' if applied == 1 else 's'} from FurnishedFinder. "
              "Check them before the agent quotes them to a guest.")
    else:
        flash("Nothing selected — your catalog is unchanged.")
    return redirect(url_for("settings"))


@app.route("/settings/import-units", methods=["POST"])
@login_required
def import_units():
    """Add properties discovered in this tenant's FurnishedFinder leads.

    Only names and areas are imported — the facts FF actually told us. Price,
    occupancy and house rules stay blank for the owner to fill, because the
    agent quotes catalog facts to real guests and must never state a number
    nobody confirmed.
    """
    tenant_id = current_user.tenant_id
    discovered = config.discover_units(tenant_id, SITE)
    if not discovered:
        flash("No new properties found in your leads yet — run Check now first.")
        return redirect(url_for("settings"))
    units = config.get_units(tenant_id) + discovered
    config.save_settings(tenant_id, units_json=json.dumps(units, indent=2))
    flash(f"Imported {len(discovered)} propert{'y' if len(discovered) == 1 else 'ies'} "
          "from FurnishedFinder. Add price and house rules so the agent can use them.")
    return redirect(url_for("settings"))


# Started here, at the bottom, because it depends on helpers defined throughout
# this module — calling it earlier raised NameError, which the try/except then
# swallowed, leaving autopilot silently dead.
_start_background_agents()


if __name__ == "__main__":
    # threaded=True so the background scrape thread and polling requests coexist.
    # Host/port from env so the VM can bind 0.0.0.0 (behind an SSH tunnel — the
    # dashboard now has login but no TLS) without editing code. Defaults stay
    # loopback-only.
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "5050"))
    app.run(host=host, port=port, threaded=True, debug=False)
