"""Flask dashboard: view recent FurnishedFinder leads/messages and trigger a
live scrape (with in-browser OTP entry).

Run:
    .venv/bin/python dashboard.py
    open http://localhost:5000
"""
import os

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, jsonify, render_template, request

import responder
import runner
import storage

SITE = "furnishedfinder"
LIMIT = 20

app = Flask(__name__)


def _recent():
    responses = storage.get_responses(SITE)
    leads = storage.get_recent(SITE, "lead", LIMIT)
    messages = storage.get_recent(SITE, "message", LIMIT)
    for it in leads:
        it["kind"] = "lead"
        it["response"] = responses.get(it.get("id"))
    for it in messages:
        it["kind"] = "message"
        it["response"] = responses.get(it.get("id"))
    return {"leads": leads, "messages": messages}


def _item_by_id(item_id: str) -> dict | None:
    """Find a lead or message by id, tagging it with its kind."""
    for kind in ("lead", "message"):
        for it in storage.get_recent(SITE, kind, 200):
            if it.get("id") == item_id:
                it["kind"] = kind
                return it
    return None


@app.route("/")
def index():
    data = _recent()
    return render_template(
        "dashboard.html",
        account=os.getenv("FF_USERNAME", "(not set)"),
        leads=data["leads"],
        messages=data["messages"],
        state=runner.get_state(),
        has_api_key=bool(os.getenv("ANTHROPIC_API_KEY")),
    )


@app.route("/api/data")
def api_data():
    return jsonify(_recent())


@app.route("/api/status")
def api_status():
    return jsonify(runner.get_state())


@app.route("/refresh", methods=["POST"])
def refresh():
    return jsonify(runner.start_scrape())


@app.route("/otp", methods=["POST"])
def otp():
    code = request.form.get("code", "") or (request.json or {}).get("code", "")
    ok = runner.submit_otp(code)
    return jsonify({"ok": ok, "state": runner.get_state()})


def _form(*keys):
    src = request.form if request.form else (request.json or {})
    return [src.get(k, "") for k in keys]


@app.route("/responder/draft", methods=["POST"])
def responder_draft():
    """(Re-)draft a single lead or message on demand."""
    (item_id,) = _form("item_id")
    item = _item_by_id(item_id)
    if not item:
        return jsonify({"ok": False, "error": "item not found"}), 404
    try:
        d = responder.evaluate_lead(item)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    storage.save_response(
        SITE, item["kind"], item_id,
        status="draft" if d.get("fit") else "skipped",
        unit_id=d.get("unit_id"), reason=d.get("reason"),
        draft=d.get("draft"), confidence=d.get("confidence"),
        tenant_email=d.get("tenant_email"),
    )
    return jsonify({"ok": True, "response": storage.get_responses(SITE).get(item_id)})


@app.route("/responder/send", methods=["POST"])
def responder_send():
    """One-click approve → send the (possibly edited) draft (lead or message)."""
    item_id, text = _form("item_id", "text")
    item = _item_by_id(item_id)
    if not item:
        return jsonify({"ok": False, "error": "item not found"}), 404
    if not (text or "").strip():
        return jsonify({"ok": False, "error": "empty reply"}), 400
    state = runner.send_reply(SITE, item, text)
    return jsonify({"ok": True, "state": state})


@app.route("/responder/dismiss", methods=["POST"])
def responder_dismiss():
    (item_id,) = _form("item_id")
    storage.update_response(SITE, item_id, status="dismissed")
    return jsonify({"ok": True})


if __name__ == "__main__":
    # threaded=True so the background scrape thread and polling requests coexist.
    # Host/port from env so the VM can bind 0.0.0.0 (behind an SSH tunnel — the
    # dashboard has no auth) without editing code. Defaults stay loopback-only.
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "5050"))
    app.run(host=host, port=port, threaded=True, debug=False)
