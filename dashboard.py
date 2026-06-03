"""Flask dashboard: view recent FurnishedFinder leads/messages and trigger a
live scrape (with in-browser OTP entry).

Multi-tenant: every request is scoped to the logged-in user's tenant. Scraping
and sending are gated to the *operator* tenant until each tenant gets its own
FurnishedFinder login (Phase 3); other tenants see an empty dashboard.

Run:
    .venv/bin/python dashboard.py
    open http://localhost:5050
"""
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

import config
import crypto
import ff_account
import models
import responder
import runner
import storage

SITE = "furnishedfinder"
LIMIT = 20

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or ""
if not app.secret_key:
    # Sessions can't be signed without a key. Fail loud rather than ship an
    # insecure default — set SECRET_KEY in .env (see .env.example).
    raise RuntimeError("SECRET_KEY not set in .env — required for login sessions.")

# Provision the operator tenant + login (from OPERATOR_EMAIL/PASSWORD) on boot.
models.ensure_operator()

login_manager = LoginManager(app)
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id):
    return models.get_user_by_id(user_id)


def _can_scrape() -> bool:
    """A tenant may scrape/send once they've connected an FF account; the
    operator is grandfathered in via the FF_USERNAME env."""
    return current_user.is_operator or ff_account.is_connected(current_user.tenant_id)


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
        alt_text='No account? <a href="%s">Sign up</a>' % url_for("signup"),
    )


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
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


@app.route("/")
@login_required
def index():
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
        state=runner.get_state(current_user.tenant_id),
        has_api_key=bool(os.getenv("ANTHROPIC_API_KEY")),
    )


@app.route("/api/data")
@login_required
def api_data():
    return jsonify(_recent(current_user.tenant_id))


@app.route("/api/status")
@login_required
def api_status():
    return jsonify(runner.get_state(current_user.tenant_id))


@app.route("/refresh", methods=["POST"])
@login_required
@scrape_allowed
def refresh():
    return jsonify(runner.start_scrape(current_user.tenant_id))


@app.route("/otp", methods=["POST"])
@login_required
@scrape_allowed
def otp():
    code = request.form.get("code", "") or (request.json or {}).get("code", "")
    ok = runner.submit_otp(current_user.tenant_id, code)
    return jsonify({"ok": ok, "state": runner.get_state(current_user.tenant_id)})


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
    access. FF login is passwordless (email + OTP), so only the email is kept."""
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
    flash("FurnishedFinder account connected — click “Check now” to fetch your leads.")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Per-tenant settings (units, template, identity)
# ---------------------------------------------------------------------------


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
            return render_template(
                "settings.html",
                account=current_user.email,
                settings={
                    "host_name": request.form.get("host_name", ""),
                    "units_json": units_text,
                    "template": request.form.get("template", ""),
                    "from_email": request.form.get("from_email", ""),
                    "reply_channels": request.form.get("reply_channels", ""),
                },
            )
        config.save_settings(
            tenant_id,
            host_name=request.form.get("host_name", "").strip(),
            units_json=units_text,
            template=request.form.get("template", ""),
            from_email=request.form.get("from_email", "").strip(),
            reply_channels=request.form.get("reply_channels", "").strip() or "platform,email",
        )
        flash("Saved.")
        return redirect(url_for("settings"))
    return render_template(
        "settings.html",
        account=current_user.email,
        settings=config.get_settings(tenant_id),
    )


if __name__ == "__main__":
    # threaded=True so the background scrape thread and polling requests coexist.
    # Host/port from env so the VM can bind 0.0.0.0 (behind an SSH tunnel — the
    # dashboard now has login but no TLS) without editing code. Defaults stay
    # loopback-only.
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "5050"))
    app.run(host=host, port=port, threaded=True, debug=False)
