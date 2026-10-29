"""Time-based one-time passwords (RFC 6238), for two-factor sign-in.

Small enough to own rather than depend on: HOTP (RFC 4226) is an HMAC of a
counter, truncated to six digits, and TOTP makes the counter the number of
30-second steps since the epoch. Any authenticator app (Google
Authenticator, 1Password, Authy...) produces the same codes from the same
secret.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

STEP = 30
DIGITS = 6
# Codes from one step either side are accepted, for clock drift and for the
# seconds it takes to type a code in.
WINDOW = 1


def new_secret() -> str:
    """A random 160-bit secret, base32 as authenticator apps expect it."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def hotp(key: bytes, counter: int, digits: int = DIGITS, digest=hashlib.sha1) -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    offset = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % 10**digits).zfill(digits)


def totp(secret: str, at: float | None = None, digits: int = DIGITS) -> str:
    moment = time.time() if at is None else at
    return hotp(_key(secret), int(moment // STEP), digits)


def verify(secret: str, code: str, at: float | None = None) -> bool:
    """Whether ``code`` is valid for ``secret`` now (or at ``at``)."""
    code = (code or "").strip().replace(" ", "")
    if not (code.isdigit() and len(code) == DIGITS):
        return False
    moment = time.time() if at is None else at
    counter = int(moment // STEP)
    key = _key(secret)
    return any(
        hmac.compare_digest(hotp(key, counter + drift), code)
        for drift in range(-WINDOW, WINDOW + 1)
    )


def provisioning_uri(secret: str, account: str, issuer: str = "CollabDoc") -> str:
    """The otpauth:// URI an authenticator app imports (usually via a QR code)."""
    label = urllib.parse.quote("%s:%s" % (issuer, account))
    query = urllib.parse.urlencode(
        {"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": DIGITS, "period": STEP}
    )
    return "otpauth://totp/%s?%s" % (label, query)
