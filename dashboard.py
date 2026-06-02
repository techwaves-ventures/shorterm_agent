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


def operator_required(fn):
    """Block non-operator tenants from scrape/send routes (Phase 1 boundary)."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_operator:
            abort(403, "FurnishedFinder connection coming soon for your account.")
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
        leads=data["leads"],
        messages=data["messages"],
        state=runner.get_state(),
        has_api_key=bool(os.getenv("ANTHROPIC_API_KEY")),
    )


@app.route("/api/data")
@login_required
def api_data():
    return jsonify(_recent(current_user.tenant_id))


@app.route("/api/status")
@login_required
def api_status():
    return jsonify(runner.get_state())


@app.route("/refresh", methods=["POST"])
@login_required
@operator_required
def refresh():
    return jsonify(runner.start_scrape(current_user.tenant_id))


@app.route("/otp", methods=["POST"])
@login_required
@operator_required
def otp():
    code = request.form.get("code", "") or (request.json or {}).get("code", "")
    ok = runner.submit_otp(code)
    return jsonify({"ok": ok, "state": runner.get_state()})


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
        d = responder.evaluate_lead(item)
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
@operator_required
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


if __name__ == "__main__":
    # threaded=True so the background scrape thread and polling requests coexist.
    # Host/port from env so the VM can bind 0.0.0.0 (behind an SSH tunnel — the
    # dashboard now has login but no TLS) without editing code. Defaults stay
    # loopback-only.
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "5050"))
    app.run(host=host, port=port, threaded=True, debug=False)
