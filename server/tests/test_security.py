"""Two-factor sign-in (TOTP) and personal API tokens."""
from __future__ import annotations

import hashlib

from app import totp
from app.db import SessionLocal
from app.models import ApiToken

PASSWORD = "correct-horse-1"


# -- TOTP ---------------------------------------------------------------------------


def test_totp_matches_the_rfc_6238_test_vectors():
    # RFC 6238 appendix B, SHA-1: the secret is ASCII "12345678901234567890".
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    vectors = {59: "94287082", 1111111109: "07081804", 1234567890: "89005924", 2000000000: "69279037"}
    for at, code in vectors.items():
        assert totp.totp(secret, at=at, digits=8) == code


def test_totp_accepts_one_step_of_drift_and_nothing_else():
    secret = totp.new_secret()
    now = 1_700_000_000
    assert totp.verify(secret, totp.totp(secret, at=now), at=now)
    assert totp.verify(secret, totp.totp(secret, at=now - 30), at=now)
    assert not totp.verify(secret, totp.totp(secret, at=now - 90), at=now)
    assert not totp.verify(secret, "12345", at=now)
    assert not totp.verify(secret, "abcdef", at=now)


def _enable(account) -> str:
    setup = account.post("/api/auth/2fa/setup")
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    assert setup.json()["uri"].startswith("otpauth://totp/CollabDoc")
    assert account.post("/api/auth/2fa/enable", json={"code": "000000"}).status_code == 400
    assert account.post("/api/auth/2fa/enable", json={"code": totp.totp(secret)}).status_code == 204
    return secret


def test_two_factor_sign_in_needs_the_password_then_a_code(client, alice):
    secret = _enable(alice)
    assert alice.get("/api/auth/me").json()["totp_enabled"] is True

    step1 = client.post("/api/auth/login", json={"email": alice.email, "password": PASSWORD}).json()
    assert step1["mfa_required"] is True and "access_token" not in step1
    # The challenge token is not a session.
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer " + step1["mfa_token"]}).status_code == 401

    wrong = client.post("/api/auth/2fa/verify", json={"mfa_token": step1["mfa_token"], "code": "000000"})
    assert wrong.status_code == 400
    right = client.post("/api/auth/2fa/verify", json={"mfa_token": step1["mfa_token"], "code": totp.totp(secret)})
    assert right.status_code == 200
    token = right.json()["access_token"]
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer " + token}).json()["id"] == alice.id


def test_two_factor_can_be_turned_off_with_a_code(client, alice):
    secret = _enable(alice)
    assert alice.post("/api/auth/2fa/disable", json={"code": "000000"}).status_code == 400
    assert alice.post("/api/auth/2fa/disable", json={"code": totp.totp(secret)}).status_code == 204
    login = client.post("/api/auth/login", json={"email": alice.email, "password": PASSWORD}).json()
    assert "access_token" in login


def test_a_forged_challenge_token_is_refused(client, alice):
    _enable(alice)
    forged = client.post("/api/auth/2fa/verify", json={"mfa_token": alice.token, "code": "123456"})
    assert forged.status_code == 401


# -- API tokens ---------------------------------------------------------------------------


def test_api_tokens_work_as_bearer_tokens_until_revoked(client, alice):
    created = alice.post("/api/me/tokens", json={"name": "CI script"})
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    assert token.startswith("cd_") and created.json()["prefix"] == token[:10]

    headers = {"Authorization": "Bearer " + token}
    assert client.get("/api/auth/me", headers=headers).json()["id"] == alice.id
    document_id = alice.new_document("Via API")
    assert client.get("/api/documents/%s" % document_id, headers=headers).status_code == 200
    # A token cannot mint more tokens.
    assert client.post("/api/me/tokens", headers=headers, json={"name": "x"}).status_code == 403

    listed = alice.get("/api/me/tokens").json()
    assert [t["name"] for t in listed] == ["CI script"] and listed[0]["last_used_at"] is not None
    assert "token" not in listed[0]

    assert alice.delete("/api/me/tokens/%s" % listed[0]["id"]).status_code == 204
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    assert alice.get("/api/me/tokens").json() == []


def test_api_tokens_are_stored_hashed(alice):
    token = alice.post("/api/me/tokens", json={"name": "hash check"}).json()["token"]
    with SessionLocal() as db:
        stored = db.query(ApiToken).filter(ApiToken.user_id == alice.id).one()
        assert stored.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert token not in (stored.token_hash, stored.prefix)


def test_someone_elses_token_cannot_be_revoked(alice, bob):
    token_id = alice.post("/api/me/tokens", json={"name": "mine"}).json()["id"]
    assert bob.delete("/api/me/tokens/%s" % token_id).status_code == 404
