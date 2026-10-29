"""Sign in with GitHub or Google (SRS 5.3 "OAuth, JWT").

The authorization-code flow, ending in the same JWT a password sign-in
issues, so nothing downstream knows or cares how someone signed in.

Three things keep the flow honest:

* ``state`` is a short-lived token signed with the server's key, so a
  callback the server did not start is refused;
* a nonce in an HttpOnly cookie must match the one inside ``state``, which
  binds the callback to the browser that began it (login CSRF);
* PKCE: the code verifier never leaves the server and that cookie, so a
  stolen authorization code is useless on its own.

The token goes back to the page in the URL fragment, which browsers never
send to a server, rather than in a query string that lands in access logs.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..models import OAuthAccount, User
from ..ratelimit import RateLimiter, client_key, limiter
from ..security import create_access_token, pick_presence_color

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE = "collabdoc_oauth"
STATE_SECONDS = 600


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    authorize_url: str
    token_url: str
    scope: str


PROVIDERS = {
    "github": Provider(
        "github",
        "GitHub",
        "https://github.com/login/oauth/authorize",
        "https://github.com/login/oauth/access_token",
        "read:user user:email",
    ),
    "google": Provider(
        "google",
        "Google",
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        "openid email profile",
    ),
}


def credentials(settings: Settings, provider: str) -> tuple[str, str]:
    return (
        getattr(settings, "%s_client_id" % provider, ""),
        getattr(settings, "%s_client_secret" % provider, ""),
    )


def enabled(settings: Settings) -> list[Provider]:
    return [p for name, p in PROVIDERS.items() if all(credentials(settings, name))]


def _provider(name: str) -> Provider:
    settings = get_settings()
    provider = PROVIDERS.get(name)
    if provider is None or not all(credentials(settings, name)):
        raise HTTPException(status_code=404, detail="That sign-in provider is not configured")
    return provider


def _redirect_uri(request: Request, provider: Provider) -> str:
    base = get_settings().public_url.rstrip("/") or str(request.base_url).rstrip("/")
    return "%s/api/auth/oauth/%s/callback" % (base, provider.name)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# -- talking to the provider ---------------------------------------------------


def _request(method: str, url: str, data: dict | None, headers: dict) -> dict:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:  # noqa: S310 - fixed https URLs
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise OAuthError("%s answered %d" % (urllib.parse.urlparse(url).netloc, exc.code)) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise OAuthError("Could not reach %s" % urllib.parse.urlparse(url).netloc) from exc


async def http_json(method: str, url: str, data: dict | None = None, headers: dict | None = None):
    """One JSON request to a provider, off the event loop. Tests replace this."""
    return await asyncio.to_thread(
        _request, method, url, data, {"Accept": "application/json", **(headers or {})}
    )


class OAuthError(Exception):
    pass


@dataclass
class Identity:
    subject: str
    email: str | None  # only when the provider says it is verified
    name: str
    avatar_url: str | None


async def fetch_identity(provider: Provider, access_token: str) -> Identity:
    auth = {"Authorization": "Bearer %s" % access_token}
    if provider.name == "github":
        profile = await http_json("GET", "https://api.github.com/user", headers=auth)
        emails = await http_json("GET", "https://api.github.com/user/emails", headers=auth)
        email = next(
            (e["email"] for e in emails if e.get("primary") and e.get("verified")), None
        ) if isinstance(emails, list) else None
        return Identity(
            subject=str(profile["id"]),
            email=email,
            name=profile.get("name") or profile.get("login") or "GitHub user",
            avatar_url=profile.get("avatar_url"),
        )
    info = await http_json("GET", "https://openidconnect.googleapis.com/v1/userinfo", headers=auth)
    return Identity(
        subject=str(info["sub"]),
        email=info.get("email") if info.get("email_verified") else None,
        name=info.get("name") or (info.get("email") or "Google user").split("@")[0],
        avatar_url=info.get("picture"),
    )


def link_user(db: Session, provider: Provider, identity: Identity) -> User:
    """The local account for an external identity, creating one if needed.

    An identity already linked signs in as its user. Otherwise a *verified*
    email joins an existing account with that address -- an unverified one
    could be anyone's, so it never does -- and failing that a new account is
    made with no password, usable only through the provider.
    """
    linked = db.scalars(
        select(OAuthAccount).where(
            OAuthAccount.provider == provider.name, OAuthAccount.subject == identity.subject
        )
    ).first()
    if linked is not None:
        return db.get(User, linked.user_id)

    user = None
    if identity.email:
        user = db.scalars(select(User).where(User.email == identity.email.lower())).first()
    if user is None:
        email = (identity.email or "%s-%s@users.noreply.collabdoc" % (provider.name, identity.subject)).lower()
        user = User(
            email=email,
            name=identity.name[:120],
            avatar_url=identity.avatar_url,
            # Not a valid hash: password sign-in can never match it.
            password_hash="oauth$%s" % provider.name,
            presence_color=pick_presence_color(db.scalar(select(func.count(User.id))) or 0),
        )
        db.add(user)
        db.flush()
    db.add(OAuthAccount(provider=provider.name, subject=identity.subject, user_id=user.id))
    db.commit()
    db.refresh(user)
    return user


# -- routes --------------------------------------------------------------------


@router.get("/providers")
def providers() -> list[dict]:
    """Sign-in providers this server is configured for."""
    return [{"id": p.name, "label": p.label} for p in enabled(get_settings())]


@router.get("/oauth/{name}/start", include_in_schema=False)
def start(name: str, request: Request) -> RedirectResponse:
    provider = _provider(name)
    settings = get_settings()
    nonce = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(48)
    state = jwt.encode(
        {"n": nonce, "p": provider.name, "exp": int(time.time()) + STATE_SECONDS},
        settings.secret_key,
        algorithm=settings.algorithm,
    )
    params = {
        "client_id": credentials(settings, provider.name)[0],
        "redirect_uri": _redirect_uri(request, provider),
        "response_type": "code",
        "scope": provider.scope,
        "state": state,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    }
    if provider.name == "google":
        params["prompt"] = "select_account"
    response = RedirectResponse("%s?%s" % (provider.authorize_url, urllib.parse.urlencode(params)), 302)
    response.set_cookie(
        COOKIE,
        "%s.%s" % (nonce, verifier),
        max_age=STATE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/api/auth/oauth",
    )
    return response


def _back_to_app(fragment: dict) -> RedirectResponse:
    response = RedirectResponse("/#%s" % urllib.parse.urlencode(fragment), 302)
    response.delete_cookie(COOKIE, path="/api/auth/oauth")
    return response


@router.get("/oauth/{name}/callback", include_in_schema=False)
async def callback(
    name: str,
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: Session = Depends(get_db),
    limits: RateLimiter = Depends(limiter),
) -> RedirectResponse:
    limits.check("auth:%s" % client_key(request), get_settings().auth_rate_limit)
    provider = _provider(name)
    if error:
        return _back_to_app({"oauth_error": "%s sign-in was cancelled" % provider.label})
    settings = get_settings()
    try:
        claims = jwt.decode(state, settings.secret_key, algorithms=[settings.algorithm])
    except jwt.PyJWTError:
        return _back_to_app({"oauth_error": "That sign-in link expired. Try again."})
    nonce, _, verifier = request.cookies.get(COOKIE, "").partition(".")
    if claims.get("p") != provider.name or not nonce or not secrets.compare_digest(nonce, claims.get("n", "")):
        return _back_to_app({"oauth_error": "Sign-in must finish in the browser that started it."})
    if not code:
        return _back_to_app({"oauth_error": "%s did not return a sign-in code" % provider.label})

    client_id, client_secret = credentials(settings, provider.name)
    try:
        token = await http_json(
            "POST",
            provider.token_url,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": _redirect_uri(request, provider),
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
        )
        access_token = token.get("access_token")
        if not access_token:
            raise OAuthError("%s refused the sign-in code" % provider.label)
        identity = await fetch_identity(provider, access_token)
    except (OAuthError, KeyError) as exc:
        return _back_to_app({"oauth_error": str(exc) or "%s sign-in failed" % provider.label})

    user = link_user(db, provider, identity)
    if user.totp_enabled:
        # Two-factor sign-in applies however the first factor was proven.
        from .security import mfa_token

        return _back_to_app({"mfa_token": mfa_token(user.id)})
    return _back_to_app({"oauth_token": create_access_token(user.id)})
