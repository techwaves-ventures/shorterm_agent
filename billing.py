"""Stripe subscription billing — with a safe demo stub when no keys are set.

Design goals for the demo SaaS:
- **Never hardcode keys.** All Stripe secrets come from env (STRIPE_SECRET_KEY,
  STRIPE_WEBHOOK_SECRET, STRIPE_PRICE_*). Nothing is committed.
- **Graceful degradation.** When STRIPE_SECRET_KEY is absent (the default demo
  posture), the app runs in *demo mode*: "subscribing" marks the tenant as
  `Pilot (demo)` locally and no live Stripe call is made. This lets Sagiv walk
  the whole pricing→checkout→status flow with zero secrets.
- **Per-tenant state.** Subscription status lives in the shared leads.db, one row
  per tenant, so the dashboard/settings can show "Active"/"Pilot (demo)"/none.

Routes are exposed as a Flask blueprint (`billing_bp`) registered in dashboard.py.
"""
import os
import sqlite3
from datetime import datetime

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from storage import DB_PATH

# Optional dependency — the app must import and run without `stripe` installed.
try:
    import stripe  # type: ignore
except Exception:  # pragma: no cover - import guard
    stripe = None


# ---------------------------------------------------------------------------
# Plans (pricing hypothesis from the VEN-14 GTM package — validate in pilot).
# `price_env` names the env var holding the live Stripe Price ID for that tier.
# ---------------------------------------------------------------------------
PLANS = [
    {
        "key": "starter",
        "name": "Starter",
        "price": "$39",
        "cadence": "/mo",
        "blurb": "Solo host, up to 2 units.",
        "features": [
            "Twice-daily lead checks",
            "AI drafts for leads & messages",
            "One-click platform + email send",
            "Human approval on every reply",
        ],
        "price_env": "STRIPE_PRICE_STARTER",
    },
    {
        "key": "pro",
        "name": "Pro",
        "price": "$99",
        "cadence": "/mo",
        "blurb": "Multi-unit host / small PM, up to 10 units.",
        "features": [
            "Everything in Starter",
            "More frequent checks",
            "All units, priority support",
            "Notifications / webhook",
        ],
        "price_env": "STRIPE_PRICE_PRO",
        "highlight": True,
    },
    {
        "key": "portfolio",
        "name": "Portfolio",
        "price": "Custom",
        "cadence": "",
        "blurb": "10+ units / property manager.",
        "features": [
            "Multiple hosts / accounts",
            "White-glove onboarding",
            "SLA & priority support",
        ],
        "price_env": None,  # sales-assisted; no self-serve checkout
        "contact": True,
    },
]

_PLAN_BY_KEY = {p["key"]: p for p in PLANS}


def demo_mode() -> bool:
    """True when no live Stripe secret is configured — the safe default."""
    return not (os.getenv("STRIPE_SECRET_KEY") or "").strip() or stripe is None


def _init_stripe() -> bool:
    """Point the stripe SDK at our secret key. Returns False in demo mode."""
    if demo_mode():
        return False
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    return True


# ---------------------------------------------------------------------------
# Per-tenant subscription state
# ---------------------------------------------------------------------------


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute(
        """CREATE TABLE IF NOT EXISTS subscriptions (
            tenant_id TEXT PRIMARY KEY,
            plan TEXT,
            status TEXT,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            current_period_end TEXT,
            demo INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    return c


def get_subscription(tenant_id: str) -> dict:
    """Return this tenant's subscription state (status 'none' if never subscribed)."""
    with _conn() as c:
        row = c.execute(
            """SELECT plan, status, stripe_customer_id, stripe_subscription_id,
                      current_period_end, demo
               FROM subscriptions WHERE tenant_id=?""",
            (tenant_id,),
        ).fetchone()
    if not row:
        return {"plan": None, "status": "none", "demo": False,
                "customer_id": None, "subscription_id": None, "current_period_end": None}
    return {
        "plan": row[0], "status": row[1] or "none",
        "customer_id": row[2], "subscription_id": row[3],
        "current_period_end": row[4], "demo": bool(row[5]),
    }


def set_subscription(tenant_id: str, **fields) -> None:
    """Upsert subscription fields for a tenant."""
    allowed = ("plan", "status", "stripe_customer_id", "stripe_subscription_id",
               "current_period_end", "demo")
    current = get_subscription(tenant_id)
    merged = {
        "plan": current["plan"], "status": current["status"],
        "stripe_customer_id": current["customer_id"],
        "stripe_subscription_id": current["subscription_id"],
        "current_period_end": current["current_period_end"],
        "demo": 1 if current["demo"] else 0,
    }
    for k, v in fields.items():
        if k in allowed:
            merged[k] = v
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        c.execute(
            """INSERT INTO subscriptions
                 (tenant_id, plan, status, stripe_customer_id, stripe_subscription_id,
                  current_period_end, demo, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(tenant_id) DO UPDATE SET
                 plan=excluded.plan, status=excluded.status,
                 stripe_customer_id=excluded.stripe_customer_id,
                 stripe_subscription_id=excluded.stripe_subscription_id,
                 current_period_end=excluded.current_period_end,
                 demo=excluded.demo, updated_at=excluded.updated_at""",
            (tenant_id, merged["plan"], merged["status"], merged["stripe_customer_id"],
             merged["stripe_subscription_id"], merged["current_period_end"],
             merged["demo"], now),
        )


def status_label(sub: dict) -> str:
    """Human label for the header/settings badge."""
    if sub["status"] == "none":
        return "No plan"
    plan = _PLAN_BY_KEY.get(sub["plan"], {}).get("name", sub["plan"] or "")
    if sub["demo"]:
        return f"Pilot (demo) · {plan}".rstrip(" ·")
    return f"{sub['status'].title()} · {plan}".rstrip(" ·")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
billing_bp = Blueprint("billing", __name__)


@billing_bp.route("/billing")
@login_required
def billing_home():
    sub = get_subscription(current_user.tenant_id)
    return render_template(
        "billing.html",
        account=current_user.email,
        plans=PLANS,
        subscription=sub,
        status_label=status_label(sub),
        demo=demo_mode(),
    )


@billing_bp.route("/billing/checkout", methods=["POST"])
@login_required
def checkout():
    tenant_id = current_user.tenant_id
    plan_key = (request.form.get("plan") or "").strip()
    plan = _PLAN_BY_KEY.get(plan_key)
    if not plan or plan.get("contact"):
        flash("That plan is sales-assisted — email us to get set up.")
        return redirect(url_for("billing.billing_home"))

    # Demo mode: no live Stripe call — mark the tenant as an active pilot locally.
    if demo_mode():
        set_subscription(tenant_id, plan=plan_key, status="active", demo=1)
        flash(f"Demo: you're now on the {plan['name']} plan (Pilot / demo — no card charged).")
        return redirect(url_for("billing.billing_home"))

    # Live mode: create a Stripe Checkout Session for the tenant's Price.
    _init_stripe()
    price_id = (os.getenv(plan["price_env"] or "") or "").strip()
    if not price_id:
        flash(f"Live billing isn't configured for {plan['name']} "
              f"(set {plan['price_env']}). No charge made.")
        return redirect(url_for("billing.billing_home"))
    base = _base_url()
    sub = get_subscription(tenant_id)
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer=sub["customer_id"] or None,
            customer_email=None if sub["customer_id"] else current_user.email,
            client_reference_id=str(tenant_id),
            metadata={"tenant_id": str(tenant_id), "plan": plan_key},
            success_url=f"{base}{url_for('billing.success')}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}{url_for('billing.billing_home')}",
        )
    except Exception as e:  # Stripe/network error — never 500 the demo.
        current_app.logger.exception("Stripe checkout failed")
        flash(f"Could not start checkout: {e}")
        return redirect(url_for("billing.billing_home"))
    return redirect(session.url, code=303)


@billing_bp.route("/billing/portal", methods=["POST"])
@login_required
def portal():
    tenant_id = current_user.tenant_id
    if demo_mode():
        flash("Demo mode: the Stripe customer portal opens here once live keys are set.")
        return redirect(url_for("billing.billing_home"))
    _init_stripe()
    sub = get_subscription(tenant_id)
    if not sub["customer_id"]:
        flash("No Stripe customer yet — subscribe to a plan first.")
        return redirect(url_for("billing.billing_home"))
    try:
        session = stripe.billing_portal.Session.create(
            customer=sub["customer_id"],
            return_url=f"{_base_url()}{url_for('billing.billing_home')}",
        )
    except Exception as e:
        current_app.logger.exception("Stripe portal failed")
        flash(f"Could not open the customer portal: {e}")
        return redirect(url_for("billing.billing_home"))
    return redirect(session.url, code=303)


@billing_bp.route("/billing/success")
@login_required
def success():
    """Return landing after Checkout. The webhook is the source of truth; this
    just optimistically reflects the new plan so the UI updates immediately."""
    session_id = request.args.get("session_id")
    if session_id and not demo_mode():
        _init_stripe()
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            set_subscription(
                current_user.tenant_id,
                plan=(session.get("metadata") or {}).get("plan"),
                status="active",
                stripe_customer_id=session.get("customer"),
                stripe_subscription_id=session.get("subscription"),
                demo=0,
            )
        except Exception:
            current_app.logger.exception("Could not confirm checkout session")
    flash("Thanks — your subscription is active.")
    return redirect(url_for("billing.billing_home"))


@billing_bp.route("/billing/webhook", methods=["POST"])
def webhook():
    """Stripe webhook: keep per-tenant subscription status in sync. Public (no
    login) but signature-verified when STRIPE_WEBHOOK_SECRET is set."""
    if demo_mode():
        return ("demo mode — webhooks disabled", 200)
    _init_stripe()
    payload = request.get_data()
    secret = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        if secret:
            event = stripe.Webhook.construct_event(payload, sig, secret)
        else:  # no secret configured — accept unverified (dev only)
            import json
            event = json.loads(payload or b"{}")
    except Exception as e:
        current_app.logger.warning("Bad Stripe webhook: %s", e)
        return ("bad signature", 400)

    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    tenant_id = (obj.get("metadata") or {}).get("tenant_id") or obj.get("client_reference_id")
    try:
        if etype == "checkout.session.completed" and tenant_id:
            set_subscription(
                tenant_id, plan=(obj.get("metadata") or {}).get("plan"),
                status="active", stripe_customer_id=obj.get("customer"),
                stripe_subscription_id=obj.get("subscription"), demo=0,
            )
        elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
            # Look up the tenant by stored customer id.
            cust = obj.get("customer")
            with _conn() as c:
                row = c.execute(
                    "SELECT tenant_id FROM subscriptions WHERE stripe_customer_id=?",
                    (cust,),
                ).fetchone()
            if row:
                status = "canceled" if etype.endswith("deleted") else obj.get("status", "active")
                cpe = obj.get("current_period_end")
                set_subscription(
                    row[0], status=status,
                    current_period_end=str(cpe) if cpe else None, demo=0,
                )
    except Exception:
        current_app.logger.exception("Webhook handling error")
        return ("error", 500)
    return ("ok", 200)


def _base_url() -> str:
    """Public base URL for Stripe redirects — PUBLIC_BASE_URL env or the request root."""
    base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    return base or request.url_root.rstrip("/")
