"""Password hashing, JWT issuance, and presence colours (WBS 3.1, SRS 5.3)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from .config import get_settings

_PBKDF2_ROUNDS = 260_000
_SALT_BYTES = 16

# Presence anchors (SRS 6.1). Chosen to stay distinguishable side by side and
# to keep enough contrast against the editor's paper-white background.
PRESENCE_PALETTE = (
    "#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed",
    "#db2777", "#0891b2", "#65a30d", "#ea580c", "#4f46e5",
)


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256, stored as ``pbkdf2_sha256$rounds$salt$hash``."""
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return "pbkdf2_sha256$%d$%s$%s" % (
        _PBKDF2_ROUNDS,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_b64, digest_b64 = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            base64.b64decode(salt_b64),
            int(rounds),
        )
    except (ValueError, TypeError):
        return False
    # Constant-time: a timing difference here leaks how much of a guess matched.
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


def create_access_token(user_id: str, expires_minutes: int | None = None) -> str:
    settings = get_settings()
    minutes = expires_minutes or settings.access_token_minutes
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes)).timestamp()),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_access_token(token: str) -> str | None:
    """The subject of a valid token, or ``None`` if it is not usable."""
    settings = get_settings()
    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.algorithm]
        )
    except jwt.PyJWTError:
        return None
    subject = payload.get("sub")
    return subject if isinstance(subject, str) else None


def pick_presence_color(taken: int) -> str:
    return PRESENCE_PALETTE[taken % len(PRESENCE_PALETTE)]


def new_share_token() -> str:
    return secrets.token_urlsafe(32)[:43]
