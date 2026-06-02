"""Users & tenants — the auth/multi-tenant spine.

Lives in the same SQLite file as the lead data (storage.DB_PATH) so a single
backup captures everything. A *tenant* is one host's workspace; a *user* logs in
and belongs to exactly one tenant. Tenant 1 is the bootstrap *operator* — the
original single-user account whose data predates multi-tenancy.

Sending/scraping is gated to the operator until Phase 3 gives each tenant its own
FurnishedFinder login; see dashboard.py.
"""
import os
import sqlite3

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

import config
from storage import DB_PATH

OPERATOR_TENANT_ID = "1"


def _conn() -> sqlite3.Connection:
    """Open the shared DB and ensure the auth tables exist (idempotent)."""
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA foreign_keys = ON")
    c.execute(
        """CREATE TABLE IF NOT EXISTS tenants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            is_operator INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    return c


class User(UserMixin):
    """Flask-Login user. `id` is the users.id; `tenant_id` scopes all data."""

    def __init__(self, id: int, email: str, tenant_id: int, is_operator: bool):
        self.id = id
        self.email = email
        # Stored/compared as a string everywhere (storage columns are TEXT).
        self.tenant_id = str(tenant_id)
        self.is_operator = bool(is_operator)

    def get_id(self) -> str:  # Flask-Login expects a string
        return str(self.id)


def _row_to_user(row) -> User | None:
    if not row:
        return None
    user_id, email, tenant_id, is_operator = row
    return User(user_id, email, tenant_id, bool(is_operator))


_USER_SELECT = (
    "SELECT u.id, u.email, u.tenant_id, t.is_operator "
    "FROM users u JOIN tenants t ON t.id = u.tenant_id"
)


def get_user_by_id(user_id) -> User | None:
    with _conn() as c:
        row = c.execute(f"{_USER_SELECT} WHERE u.id=?", (user_id,)).fetchone()
    return _row_to_user(row)


def get_user_by_email(email: str) -> User | None:
    with _conn() as c:
        row = c.execute(f"{_USER_SELECT} WHERE u.email=?", (email,)).fetchone()
    return _row_to_user(row)


def verify_password(email: str, password: str) -> User | None:
    """Return the User if the password matches, else None."""
    with _conn() as c:
        row = c.execute(
            "SELECT password_hash FROM users WHERE email=?", (email,)
        ).fetchone()
    if not row or not check_password_hash(row[0], password):
        return None
    return get_user_by_email(email)


def create_user(email: str, password: str, tenant_name: str | None = None) -> User:
    """Create a brand-new tenant + its first user (self-serve signup).

    Raises ValueError if the email is already taken or inputs are blank.
    """
    email = (email or "").strip().lower()
    if not email or not password:
        raise ValueError("Email and password are required.")
    if get_user_by_email(email):
        raise ValueError("An account with that email already exists.")
    name = (tenant_name or email.split("@")[0]).strip() or email
    pw_hash = generate_password_hash(password)
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO tenants (name, is_operator) VALUES (?, 0)", (name,)
        )
        tenant_id = cur.lastrowid
        c.execute(
            "INSERT INTO users (tenant_id, email, password_hash) VALUES (?,?,?)",
            (tenant_id, email, pw_hash),
        )
    # Seed fresh per-tenant config (empty units + default template).
    config.seed_tenant(str(tenant_id))
    return get_user_by_email(email)


def set_password(email: str, password: str) -> bool:
    """Reset a user's password. Returns False if no such user."""
    email = (email or "").strip().lower()
    if not password:
        raise ValueError("Password is required.")
    pw_hash = generate_password_hash(password)
    with _conn() as c:
        cur = c.execute(
            "UPDATE users SET password_hash=? WHERE email=?", (pw_hash, email)
        )
        return cur.rowcount > 0


def ensure_operator() -> None:
    """Ensure tenant 1 (the operator) exists, and create its login from env
    (OPERATOR_EMAIL / OPERATOR_PASSWORD) on first run.

    All pre-existing lead data is attached to tenant 1 via the DEFAULT '1' on the
    storage tenant_id columns, so this just needs the tenant row + a login.
    """
    with _conn() as c:
        row = c.execute(
            "SELECT id FROM tenants WHERE id=?", (OPERATOR_TENANT_ID,)
        ).fetchone()
        if not row:
            name = (os.getenv("HOST_NAME") or "Operator").strip()
            # Force id=1 so it matches storage's DEFAULT '1' backfill.
            c.execute(
                "INSERT INTO tenants (id, name, is_operator) VALUES (?, ?, 1)",
                (OPERATOR_TENANT_ID, name),
            )
    # Seed the operator's config from the legacy global files/env (idempotent —
    # won't clobber edits once a settings row exists).
    config.seed_tenant(OPERATOR_TENANT_ID, from_legacy=True)

    email = (os.getenv("OPERATOR_EMAIL") or "").strip().lower()
    password = os.getenv("OPERATOR_PASSWORD") or ""
    if not email or not password:
        return  # No env creds — operator can set one via `manage.py set-password`.

    existing = get_user_by_email(email)
    if existing:
        return  # Already provisioned; don't clobber an existing password.
    with _conn() as c:
        c.execute(
            "INSERT INTO users (tenant_id, email, password_hash) VALUES (?,?,?)",
            (OPERATOR_TENANT_ID, email, generate_password_hash(password)),
        )
