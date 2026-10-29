"""Account security: two-factor sign-in and personal API tokens."""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import totp
from ..config import get_settings
from ..db import get_db
from ..deps import API_TOKEN_PREFIX, get_current_user, hash_api_token
from ..models import ApiToken, User
from ..ratelimit import RateLimiter, client_key, limiter
from ..schemas import (
    ApiTokenCreate,
    ApiTokenCreated,
    ApiTokenOut,
    MfaVerify,
    TokenResponse,
    TotpCode,
    TotpSetup,
    UserOut,
)
from ..security import create_access_token

router = APIRouter(tags=["security"])

MFA_SECONDS = 300
MAX_TOKENS = 20


# -- two-factor sign-in --------------------------------------------------------


def mfa_token(user_id: str) -> str:
    """Proof that the password step passed, good for five minutes.

    It carries no ``sub`` claim, so it can never be mistaken for a session
    token by ``decode_access_token``.
    """
    settings = get_settings()
    return jwt.encode(
        {"mfa": user_id, "exp": int(time.time()) + MFA_SECONDS, "jti": secrets.token_hex(8)},
        settings.secret_key,
        algorithm=settings.algorithm,
    )


def _check_code(user: User, code: str) -> None:
    if not user.totp_secret or not totp.verify(user.totp_secret, code):
        raise HTTPException(status_code=400, detail="That code is not valid. Check your authenticator app.")


@router.post("/api/auth/2fa/setup", response_model=TotpSetup)
def setup_2fa(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> TotpSetup:
    """Start (or restart) enrolment: a fresh secret, inactive until confirmed."""
    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="Two-factor sign-in is already on")
    user.totp_secret = totp.new_secret()
    db.commit()
    return TotpSetup(secret=user.totp_secret, uri=totp.provisioning_uri(user.totp_secret, user.email))


@router.post("/api/auth/2fa/enable", status_code=status.HTTP_204_NO_CONTENT)
def enable_2fa(
    payload: TotpCode,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limits: RateLimiter = Depends(limiter),
) -> None:
    limits.check("2fa:%s" % user.id, get_settings().auth_rate_limit)
    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="Two-factor sign-in is already on")
    _check_code(user, payload.code)
    user.totp_enabled = True
    db.commit()


@router.post("/api/auth/2fa/disable", status_code=status.HTTP_204_NO_CONTENT)
def disable_2fa(
    payload: TotpCode,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limits: RateLimiter = Depends(limiter),
) -> None:
    limits.check("2fa:%s" % user.id, get_settings().auth_rate_limit)
    if not user.totp_enabled:
        raise HTTPException(status_code=409, detail="Two-factor sign-in is not on")
    _check_code(user, payload.code)
    user.totp_enabled = False
    user.totp_secret = None
    db.commit()


@router.post("/api/auth/2fa/verify", response_model=TokenResponse)
def verify_2fa(
    payload: MfaVerify,
    request: Request,
    db: Session = Depends(get_db),
    limits: RateLimiter = Depends(limiter),
) -> TokenResponse:
    """The second sign-in step: the password step's token plus a current code."""
    settings = get_settings()
    limits.check("auth:%s" % client_key(request), settings.auth_rate_limit)
    try:
        claims = jwt.decode(payload.mfa_token, settings.secret_key, algorithms=[settings.algorithm])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Sign-in timed out. Enter your password again.") from exc
    user = db.get(User, claims.get("mfa") or "")
    if user is None or not user.totp_enabled:
        raise HTTPException(status_code=401, detail="Sign-in timed out. Enter your password again.")
    # Per account as well as per address: guessing six digits from many
    # addresses at once is still capped.
    limits.check("2fa:%s" % user.id, settings.auth_rate_limit)
    _check_code(user, payload.code)
    return TokenResponse(access_token=create_access_token(user.id), user=UserOut.model_validate(user))


# -- personal API tokens -------------------------------------------------------


def _token_out(row: ApiToken) -> ApiTokenOut:
    return ApiTokenOut(
        id=row.id, name=row.name, prefix=row.prefix, created_at=row.created_at, last_used_at=row.last_used_at
    )


@router.get("/api/me/tokens", response_model=list[ApiTokenOut])
def list_tokens(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[ApiTokenOut]:
    rows = db.scalars(
        select(ApiToken)
        .where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))
        .order_by(ApiToken.created_at.desc())
    ).all()
    return [_token_out(row) for row in rows]


@router.post("/api/me/tokens", response_model=ApiTokenCreated, status_code=status.HTTP_201_CREATED)
def create_token(
    payload: ApiTokenCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiTokenCreated:
    # A token cannot mint more tokens: a leaked one cannot entrench itself.
    if request.headers.get("Authorization", "").partition(" ")[2].startswith(API_TOKEN_PREFIX):
        raise HTTPException(status_code=403, detail="Sign in with your password to create API tokens")
    live = db.scalars(
        select(ApiToken).where(ApiToken.user_id == user.id, ApiToken.revoked_at.is_(None))
    ).all()
    if len(live) >= MAX_TOKENS:
        raise HTTPException(status_code=400, detail="You have %d tokens; revoke one first" % MAX_TOKENS)
    token = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = ApiToken(
        user_id=user.id, name=payload.name.strip(), token_hash=hash_api_token(token), prefix=token[:10]
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return ApiTokenCreated(**_token_out(row).model_dump(), token=token)


@router.delete("/api/me/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_token(token_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> None:
    row = db.get(ApiToken, token_id)
    if row is None or row.user_id != user.id or row.revoked_at is not None:
        raise HTTPException(status_code=404, detail="Token not found")
    row.revoked_at = datetime.now(timezone.utc)
    db.commit()
