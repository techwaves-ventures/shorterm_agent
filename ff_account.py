"""Per-tenant FurnishedFinder account connection.

FF login is passwordless (email + OTP code), so the only stored credential is
the FF email — encrypted at rest with crypto.Fernet. The real session artifact
is the per-tenant browser profile on disk (see check_leads._profile_dir); this
table just records which email a tenant connected and that they consented to
automated access.
"""
from datetime import datetime

import crypto
import db


def _conn() -> db.Conn:
    c = db.connect()
    c.execute(
        """CREATE TABLE IF NOT EXISTS ff_accounts (
            tenant_id TEXT PRIMARY KEY,
            username_enc TEXT NOT NULL,
            consent_at TIMESTAMP,
            connected_at TIMESTAMP
        )"""
    )
    return c


def connect(tenant_id: str, ff_email: str) -> None:
    """Store (encrypted) the tenant's FF email + consent/connect timestamps.

    Raises RuntimeError if encryption isn't configured (crypto.encrypt) or the
    email is blank.
    """
    ff_email = (ff_email or "").strip()
    if not ff_email:
        raise ValueError("FurnishedFinder email is required.")
    enc = crypto.encrypt(ff_email)  # raises if FF_CRED_KEY missing
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        c.execute(
            """INSERT INTO ff_accounts (tenant_id, username_enc, consent_at, connected_at)
               VALUES (?,?,?,?)
               ON CONFLICT(tenant_id) DO UPDATE SET
                 username_enc=excluded.username_enc,
                 consent_at=excluded.consent_at,
                 connected_at=excluded.connected_at""",
            (tenant_id, enc, now, now),
        )


def disconnect(tenant_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM ff_accounts WHERE tenant_id=?", (tenant_id,))


def is_connected(tenant_id: str) -> bool:
    """Whether the tenant has connected an FF account. Row presence only — no
    decryption — so the scrape gate works even if the key is briefly absent."""
    with _conn() as c:
        return (
            c.execute(
                "SELECT 1 FROM ff_accounts WHERE tenant_id=?", (tenant_id,)
            ).fetchone()
            is not None
        )


def get_username(tenant_id: str) -> str | None:
    """Decrypt and return the tenant's FF email, or None if not connected.
    Propagates crypto errors (bad/missing key) to the caller."""
    with _conn() as c:
        row = c.execute(
            "SELECT username_enc FROM ff_accounts WHERE tenant_id=?", (tenant_id,)
        ).fetchone()
    if not row:
        return None
    return crypto.decrypt(row[0])


def _mask(email: str) -> str:
    """jane@example.com -> j***@example.com (for UI/logs; never the full local part)."""
    if "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    head = local[0] if local else ""
    return f"{head}***@{domain}"


def status(tenant_id: str) -> dict:
    """UI-facing connection status. Decrypts the email for masking when possible;
    falls back to 'connected (locked)' if the key can't decrypt it."""
    with _conn() as c:
        row = c.execute(
            "SELECT consent_at, connected_at FROM ff_accounts WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
    if not row:
        return {"connected": False, "consent_at": None, "masked_email": None}
    masked = None
    try:
        email = get_username(tenant_id)
        masked = _mask(email) if email else None
    except Exception:
        masked = "connected (locked)"
    return {"connected": True, "consent_at": row[0], "masked_email": masked}
