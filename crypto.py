"""Fernet encryption for FurnishedFinder credentials at rest.

The key comes from the FF_CRED_KEY env var (a urlsafe base64 Fernet key). There
is deliberately NO default/fallback key — if it's unset, encrypt/decrypt raise
rather than silently using a guessable key. Plaintext is never logged.

Generate a key:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import os

from cryptography.fernet import Fernet, InvalidToken

_ENV = "FF_CRED_KEY"


def available() -> bool:
    """True if an encryption key is configured (so the connect flow is usable)."""
    return bool((os.getenv(_ENV) or "").strip())


def _fernet() -> Fernet:
    key = (os.getenv(_ENV) or "").strip()
    if not key:
        raise RuntimeError(
            f"{_ENV} not set — cannot encrypt/decrypt FF credentials. "
            "Generate one with: python -c \"from cryptography.fernet import "
            "Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        raise RuntimeError(f"{_ENV} is not a valid Fernet key.")


def encrypt(text: str) -> str:
    """Encrypt a string → urlsafe token. Raises if no key is configured."""
    return _fernet().encrypt((text or "").encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a token → string. Raises a readable error on a bad key/token;
    never echoes ciphertext or plaintext into the message."""
    try:
        return _fernet().decrypt((token or "").encode()).decode()
    except InvalidToken:
        raise RuntimeError(
            "Could not decrypt FF credential — wrong FF_CRED_KEY or corrupted "
            "value. The tenant must reconnect their account."
        )
