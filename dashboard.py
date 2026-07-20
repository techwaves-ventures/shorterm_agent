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
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)

import billing
import check_leads
import config
import crypto
import ff_account
import jobs
import models
import responder
import runner
import storage
import waitlist

SITE = "furnishedfinder"
LIMIT = 20

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or ""
if not app.secret_key:
    # Sessions can't be signed without a key. Fail loud rather than ship an
    # insecure default — set SECRET_KEY in .env (see .env.example).
    raise RuntimeError("SECRET_KEY not set in .env — required for login sessions.")


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
    for kind in ("lead", "message"):
        for it in storage.get_recent(tenant_id, SITE, kind, 200):
            if it.get("id") == item_id:
                it["kind"] = kind
                return it
    return None


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
        user = models.verify_password(email, password)
        if user:
            login_user(user)
            return redirect(url_for("index"))
        flash("Invalid email or password.")
    return render_template(
        "auth.html",
        title="Log in",
        subtitle="Sign in to your dashboard.",
        action=url_for("login"),
        alt_text='Need access? <a href="%s">Contact us</a>' % (url_for("landing") + "#pilot"),
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
    data = _recent(current_user.tenant_id)
    return render_template(
        "dashboard.html",
        account=current_user.email,
        is_operator=current_user.is_operator,
        can_scrape=_can_scrape(),
        ff_status=ff_account.status(current_user.tenant_id),
        crypto_ready=crypto.available(),
        leads=data["leads"],
        messages=data["messages"],
        state=_live_state(current_user.tenant_id),
        has_api_key=bool(os.getenv("ANTHROPIC_API_KEY")),
        billing_label=billing.status_label(billing.get_subscription(current_user.tenant_id)),
    )


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
    """One-click approve → send the (possibly edited) draft (lead or message)."""
    tenant_id = current_user.tenant_id
    item_id, text = _form("item_id", "text")
    item = _item_by_id(tenant_id, item_id)
    if not item:
        return jsonify({"ok": False, "error": "item not found"}), 404
    if not (text or "").strip():
        return jsonify({"ok": False, "error": "empty reply"}), 400
    state = runner.send_reply(tenant_id, SITE, item, text)
    return jsonify({"ok": True, "state": state})


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


def _settings_context(tenant_id: str, settings: dict) -> dict:
    """Shared template context for the settings page."""
    return {
        "account": current_user.email,
        "settings": settings,
        "ff_status": ff_account.status(tenant_id),
        "crypto_ready": crypto.available(),
        "billing_label": billing.status_label(billing.get_subscription(tenant_id)),
        "auto_send": False,  # human-in-the-loop is enforced; no auto-send in this build
    }


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    tenant_id = current_user.tenant_id
    if request.method == "POST":
        units_text = request.form.get("units_json", "")
        try:
            config.validate_units(units_text)
        except ValueError as e:
            flash(str(e))
            # Re-render with the user's unsaved edits so nothing is lost.
            edits = {
                "host_name": request.form.get("host_name", ""),
                "units_json": units_text,
                "template": request.form.get("template", ""),
                "from_email": request.form.get("from_email", ""),
                "reply_channels": request.form.get("reply_channels", ""),
                "notify_webhook": request.form.get("notify_webhook", ""),
            }
            return render_template("settings.html", **_settings_context(tenant_id, edits))
        config.save_settings(
            tenant_id,
            host_name=request.form.get("host_name", "").strip(),
            units_json=units_text,
            template=request.form.get("template", ""),
            from_email=request.form.get("from_email", "").strip(),
            reply_channels=request.form.get("reply_channels", "").strip() or "platform,email",
            notify_webhook=request.form.get("notify_webhook", "").strip(),
        )
        flash("Saved.")
        return redirect(url_for("settings"))
    return render_template(
        "settings.html",
        **_settings_context(tenant_id, config.get_settings(tenant_id)),
    )


if __name__ == "__main__":
    # threaded=True so the background scrape thread and polling requests coexist.
    # Host/port from env so the VM can bind 0.0.0.0 (behind an SSH tunnel — the
    # dashboard now has login but no TLS) without editing code. Defaults stay
    # loopback-only.
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "5050"))
    app.run(host=host, port=port, threaded=True, debug=False)
