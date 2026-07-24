"""Users & tenants — the auth/multi-tenant spine.

Lives in the same SQLite file as the lead data (storage.DB_PATH) so a single
backup captures everything. A *tenant* is one host's workspace; a *user* logs in
and belongs to exactly one tenant. Tenant 1 is the bootstrap *operator* — the
original single-user account whose data predates multi-tenancy.

Sending/scraping is gated to the operator until Phase 3 gives each tenant its own
FurnishedFinder login; see dashboard.py.
"""
import os
from datetime import datetime, timedelta

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

import config
import db

OPERATOR_TENANT_ID = "1"

# Password policy. Deliberately length-first rather than a character-class maze:
# length is what actually resists guessing, and complexity rules push people
# toward predictable substitutions.
MIN_PASSWORD_LENGTH = 10
_COMMON_PASSWORDS = {
    "password", "password1", "12345678", "123456789", "1234567890",
    "qwerty123", "letmein123", "welcome123", "shorterm123", "changeme",
}

# Login throttling: lock a single account out after this many consecutive
# failures within the window. Per-account (not per-IP) because the threat here
# is credential stuffing against a known email, and a shared office NAT
# shouldn't lock out a whole team.
MAX_LOGIN_FAILURES = 8
LOGIN_LOCKOUT_MINUTES = 15


class WeakPassword(ValueError):
    """Raised when a password fails the policy. Message is user-facing."""


def validate_password(password: str) -> None:
    """Raise WeakPassword if the password is unusable. Returns None if fine."""
    pw = password or ""
    if len(pw) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if pw.lower() in _COMMON_PASSWORDS:
        raise WeakPassword("That password is too common — please pick another.")
    if len(set(pw)) < 4:
        raise WeakPassword("Password is too repetitive — please pick another.")


def _conn() -> db.Conn:
    """Open the shared DB and ensure the auth tables exist (idempotent)."""
    c = db.connect()
    if not c.pg:
        c.execute("PRAGMA foreign_keys = ON")  # Postgres enforces FKs natively
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
    # Login throttling state. In the DB rather than process memory so it holds
    # across restarts and across multiple web processes.
    c.execute(
        """CREATE TABLE IF NOT EXISTS login_attempts (
            email TEXT PRIMARY KEY,
            failures INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT
        )"""
    )
    return c


def _now() -> datetime:
    return datetime.now()


def lockout_remaining(email: str) -> int:
    """Seconds this account stays locked, or 0 if it can attempt a login."""
    email = (email or "").strip().lower()
    if not email:
        return 0
    with _conn() as c:
        row = c.execute(
            "SELECT locked_until FROM login_attempts WHERE email=?", (email,)
        ).fetchone()
    if not row or not row[0]:
        return 0
    try:
        until = datetime.fromisoformat(row[0])
    except ValueError:
        return 0
    remaining = (until - _now()).total_seconds()
    return int(remaining) if remaining > 0 else 0


def record_login_failure(email: str) -> None:
    """Count a failed attempt and lock the account once it crosses the limit."""
    email = (email or "").strip().lower()
    if not email:
        return
    with _conn() as c:
        row = c.execute(
            "SELECT failures FROM login_attempts WHERE email=?", (email,)
        ).fetchone()
        failures = (row[0] if row else 0) + 1
        locked_until = None
        if failures >= MAX_LOGIN_FAILURES:
            locked_until = (
                _now() + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
            ).isoformat(timespec="seconds")
            failures = 0  # start a fresh count after the lockout expires
        c.execute(
            """INSERT INTO login_attempts (email, failures, locked_until)
               VALUES (?,?,?)
               ON CONFLICT(email) DO UPDATE SET
                 failures=excluded.failures, locked_until=excluded.locked_until""",
            (email, failures, locked_until),
        )


def clear_login_failures(email: str) -> None:
    """Reset throttling after a successful login."""
    email = (email or "").strip().lower()
    with _conn() as c:
        c.execute("DELETE FROM login_attempts WHERE email=?", (email,))


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
    # users.id is an integer column; Postgres (unlike SQLite) won't compare it
    # against a string param, and Flask-Login hands back the id as a string.
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    with _conn() as c:
        row = c.execute(f"{_USER_SELECT} WHERE u.id=?", (uid,)).fetchone()
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
    validate_password(password)  # raises WeakPassword (a ValueError)
    if get_user_by_email(email):
        raise ValueError("An account with that email already exists.")
    name = (tenant_name or email.split("@")[0]).strip() or email
    pw_hash = generate_password_hash(password)
    with _conn() as c:
        tenant_id = db.insert_returning_id(
            c, "INSERT INTO tenants (name, is_operator) VALUES (?, 0)", (name,)
        )
        c.execute(
            "INSERT INTO users (tenant_id, email, password_hash) VALUES (?,?,?)",
            (tenant_id, email, pw_hash),
        )
    # Seed fresh per-tenant config (empty units + default template).
    config.seed_tenant(str(tenant_id))
    return get_user_by_email(email)


RESET_TOKEN_MAX_AGE = 3600  # seconds a reset link stays valid
_RESET_SALT = "shorterm-password-reset"


def make_reset_token(email: str, secret_key: str) -> str | None:
    """Signed, expiring, single-use password-reset token. None if no such user.

    Single-use without a tokens table: the current password hash is folded into
    the signed payload, so the moment the password changes every outstanding
    token for that account stops verifying.
    """
    from itsdangerous import URLSafeTimedSerializer

    email = (email or "").strip().lower()
    with _conn() as c:
        row = c.execute(
            "SELECT password_hash FROM users WHERE email=?", (email,)
        ).fetchone()
    if not row:
        return None
    serializer = URLSafeTimedSerializer(secret_key, salt=_RESET_SALT)
    return serializer.dumps({"email": email, "h": row[0][-16:]})


def verify_reset_token(token: str, secret_key: str) -> str | None:
    """Return the email a valid reset token belongs to, else None."""
    from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

    serializer = URLSafeTimedSerializer(secret_key, salt=_RESET_SALT)
    try:
        data = serializer.loads(token or "", max_age=RESET_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    email = (data or {}).get("email")
    fingerprint = (data or {}).get("h")
    if not email or not fingerprint:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT password_hash FROM users WHERE email=?", (email,)
        ).fetchone()
    # Password already changed since the link was issued → token is spent.
    if not row or row[0][-16:] != fingerprint:
        return None
    return email


def all_tenant_ids() -> list[str]:
    """Every tenant id, as strings (storage columns are TEXT).

    Used by the autopilot scheduler to find who is owed a check.
    """
    with _conn() as c:
        rows = c.execute("SELECT id FROM tenants ORDER BY id").fetchall()
    return [str(r[0]) for r in rows]


def set_password(email: str, password: str) -> bool:
    """Reset a user's password. Returns False if no such user."""
    email = (email or "").strip().lower()
    if not password:
        raise ValueError("Password is required.")
    validate_password(password)
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
    # tenants.id is integer; pass an int so Postgres accepts the comparison and
    # the forced-id insert (SQLite is dynamically typed and wouldn't care).
    op_id = int(OPERATOR_TENANT_ID)
    with _conn() as c:
        row = c.execute("SELECT id FROM tenants WHERE id=?", (op_id,)).fetchone()
        if not row:
            name = (os.getenv("HOST_NAME") or "Operator").strip()
            # Force id=1 so it matches storage's DEFAULT '1' backfill.
            c.execute(
                "INSERT INTO tenants (id, name, is_operator) VALUES (?, ?, 1)",
                (op_id, name),
            )
            # Keep Postgres' sequence ahead of the forced id (no-op on SQLite).
            db.sync_serial(c, "tenants")
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
