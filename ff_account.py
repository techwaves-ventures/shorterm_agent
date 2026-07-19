"""Per-tenant FurnishedFinder account connection + honest connection state.

FF login is passwordless (email + OTP code), so the only stored credential is
the FF email — encrypted at rest with crypto.Fernet. The real session artifact
is the per-tenant browser profile on disk (see check_leads._profile_dir); this
table records which email a tenant connected, that they consented to automated
access, and — crucially — whether we have actually established a live FF session
for them yet.

Connection state machine (the `state` column):

    not_connected      no row — the tenant hasn't linked an email
    needs_verification email + consent saved, but no FF session verified yet
    verifying          a scrape/verification attempt is running (browser up)
    waiting_for_otp    that attempt is blocked on the tenant's one-time code
    connected          a real FF login/scrape succeeded (session established)
    error              the last verification attempt failed

Saving the email alone lands in `needs_verification`, NEVER `connected`: the app
must not claim a tenant is authenticated before an OTP/session actually
verifies. `connected` is only reached from the scrape path (runner/worker) after
`check_leads.run_scrape` completes a real browser login.
"""
from datetime import datetime

import crypto
import db

# Persistent (resting) states stored on the row. Live transient states
# (verifying / waiting_for_otp) are also written here while a run is active, but
# the authoritative live view for the UI comes from the runner/jobs state.
NOT_CONNECTED = "not_connected"
NEEDS_VERIFICATION = "needs_verification"
VERIFYING = "verifying"
WAITING_FOR_OTP = "waiting_for_otp"
CONNECTED = "connected"
ERROR = "error"

_STATE_LABELS = {
    NOT_CONNECTED: "Not connected",
    NEEDS_VERIFICATION: "Email saved — verification required",
    VERIFYING: "Verifying your FurnishedFinder login…",
    WAITING_FOR_OTP: "Waiting for your one-time code",
    CONNECTED: "Connected",
    ERROR: "Last connection attempt failed",
}

_VALID_STATES = set(_STATE_LABELS)


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
    # Idempotent migration: older databases predate the honest-state columns.
    # Existing rows are back-filled below so they don't silently read as
    # not_connected (which would hide a tenant's already-saved email).
    have = db.table_columns(c, "ff_accounts")
    for col, decl in (
        ("state", "TEXT"),
        ("verified_at", "TIMESTAMP"),
        ("last_error", "TEXT"),
    ):
        if col not in have:
            c.execute(f"ALTER TABLE ff_accounts ADD COLUMN {col} {decl}")
    if "state" not in have:
        # Legacy rows recorded `connected_at` on save (the old, misleading
        # behavior). We can't prove those had a real session, so the honest
        # migration lands them in needs_verification — one Check now re-verifies.
        c.execute(
            "UPDATE ff_accounts SET state=? WHERE state IS NULL",
            (NEEDS_VERIFICATION,),
        )
    return c


def connect(tenant_id: str, ff_email: str) -> None:
    """Save (encrypted) the tenant's FF email + consent and mark the account as
    `needs_verification`.

    This deliberately does NOT mark the account connected: passwordless FF login
    still requires a one-time code, which only happens during a scrape attempt.
    Raises ValueError on a blank email, RuntimeError if encryption isn't
    configured (crypto.encrypt).
    """
    ff_email = (ff_email or "").strip()
    if not ff_email:
        raise ValueError("FurnishedFinder email is required.")
    enc = crypto.encrypt(ff_email)  # raises if FF_CRED_KEY missing
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        c.execute(
            """INSERT INTO ff_accounts (tenant_id, username_enc, consent_at, state)
               VALUES (?,?,?,?)
               ON CONFLICT(tenant_id) DO UPDATE SET
                 username_enc=excluded.username_enc,
                 consent_at=excluded.consent_at,
                 state=excluded.state,
                 connected_at=NULL,
                 verified_at=NULL,
                 last_error=NULL""",
            (tenant_id, enc, now, NEEDS_VERIFICATION),
        )


def mark_state(tenant_id: str, state: str, error: str | None = None) -> None:
    """Advance an *existing* account row to a new connection state.

    Update-only: it never inserts a row, so the operator tenant (which has no
    ff_accounts row — it logs in via FF_USERNAME) is a safe no-op. `error` is a
    short, UI-safe message (never a raw stack trace or credential).
    """
    if state not in _VALID_STATES:
        raise ValueError(f"unknown FF account state: {state!r}")
    now = datetime.now().isoformat(timespec="seconds")
    sets = ["state=?"]
    vals: list = [state]
    if state == CONNECTED:
        sets += ["connected_at=?", "verified_at=?", "last_error=?"]
        vals += [now, now, None]
    elif state == ERROR:
        sets.append("last_error=?")
        vals.append((error or "Connection attempt failed.")[:300])
    else:
        # Transient/needs states clear any stale error banner.
        sets.append("last_error=?")
        vals.append(None)
    vals.append(tenant_id)
    with _conn() as c:
        c.execute(
            f"UPDATE ff_accounts SET {', '.join(sets)} WHERE tenant_id=?", vals
        )


def disconnect(tenant_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM ff_accounts WHERE tenant_id=?", (tenant_id,))


def has_account(tenant_id: str) -> bool:
    """Whether the tenant has linked an FF email at all. Row presence only — no
    decryption — so the scrape gate works even if the key is briefly absent.

    This is NOT the same as being connected: a tenant can have an account row in
    `needs_verification` (email saved, no session yet). Use `is_verified()` to
    ask "do we have a real FF session".
    """
    with _conn() as c:
        return (
            c.execute(
                "SELECT 1 FROM ff_accounts WHERE tenant_id=?", (tenant_id,)
            ).fetchone()
            is not None
        )


# Backward-compatible alias: older call sites (seed_demo) used `is_connected`
# to mean "has a row". The honest name is has_account(); keep the old one
# pointing at the same row-presence check so imports don't break.
is_connected = has_account


def get_state(tenant_id: str) -> str:
    """The tenant's persistent connection state (NOT_CONNECTED if no row)."""
    with _conn() as c:
        row = c.execute(
            "SELECT state FROM ff_accounts WHERE tenant_id=?", (tenant_id,)
        ).fetchone()
    if not row:
        return NOT_CONNECTED
    return row[0] or NEEDS_VERIFICATION


def is_verified(tenant_id: str) -> bool:
    """True only when a real FF session has been established (state=connected)."""
    return get_state(tenant_id) == CONNECTED


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
    """UI-facing connection status.

    `connected` means a verified FF session exists (state=connected) — the UI
    keys its "Connected" affordances off this, so a merely-saved email never
    reads as connected. Decrypts the email for masking when possible; falls back
    to a locked label if the key can't decrypt it.
    """
    with _conn() as c:
        row = c.execute(
            "SELECT consent_at, state, verified_at, last_error "
            "FROM ff_accounts WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
    if not row:
        return {
            "connected": False,
            "state": NOT_CONNECTED,
            "state_label": _STATE_LABELS[NOT_CONNECTED],
            "consent_at": None,
            "verified_at": None,
            "last_error": None,
            "masked_email": None,
        }
    consent_at, state, verified_at, last_error = row
    state = state or NEEDS_VERIFICATION
    masked = None
    try:
        email = get_username(tenant_id)
        masked = _mask(email) if email else None
    except Exception:
        masked = "connected (locked)"
    return {
        "connected": state == CONNECTED,
        "state": state,
        "state_label": _STATE_LABELS.get(state, state),
        "consent_at": consent_at,
        "verified_at": verified_at,
        "last_error": last_error,
        "masked_email": masked,
    }
