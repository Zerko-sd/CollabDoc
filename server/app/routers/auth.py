"""Authentication endpoints (WBS 3.1, SRS 5.3)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..ratelimit import RateLimiter, client_key, limiter
from ..deps import get_current_user
from ..models import User
from ..schemas import LoginRequest, MeOut, MfaChallenge, RegisterRequest, TokenResponse, UserOut
from ..security import (
    create_access_token,
    hash_password,
    pick_presence_color,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
    limits: RateLimiter = Depends(limiter),
) -> TokenResponse:
    limits.check("auth:%s" % client_key(request), get_settings().auth_rate_limit)
    email = payload.email.lower()
    if db.scalars(select(User).where(User.email == email)).first():
        raise HTTPException(status_code=409, detail="That email is already registered")

    user = User(
        email=email,
        name=payload.name.strip(),
        password_hash=hash_password(payload.password),
        presence_color=pick_presence_color(db.scalar(select(func.count(User.id))) or 0),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return TokenResponse(
        access_token=create_access_token(user.id), user=UserOut.model_validate(user)
    )


@router.post("/login", response_model=TokenResponse | MfaChallenge)
def login(
    payload: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
    limits: RateLimiter = Depends(limiter),
) -> TokenResponse | MfaChallenge:
    limits.check("auth:%s" % client_key(request), get_settings().auth_rate_limit)
    user = db.scalars(select(User).where(User.email == payload.email.lower())).first()
    # Same message either way: distinguishing them tells an attacker which
    # addresses are registered.
    # Accounts made through GitHub or Google have no password to match.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if user.totp_enabled:
        # The password alone is not enough: the second step trades this
        # short-lived token and a current code for a session.
        from .security import mfa_token

        return MfaChallenge(mfa_token=mfa_token(user.id))
    return TokenResponse(
        access_token=create_access_token(user.id), user=UserOut.model_validate(user)
    )


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(get_current_user)) -> MeOut:
    return MeOut.model_validate(user)
