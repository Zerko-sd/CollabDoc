"""Authentication and role-based access control (WBS 9.1, covers WBS 3.1/3.2)."""
from __future__ import annotations


def test_register_returns_a_usable_token(client, alice):
    response = alice.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json()["email"] == alice.email
    assert response.json()["presence_color"].startswith("#")


def test_duplicate_email_is_rejected(client, alice):
    response = client.post(
        "/api/auth/register",
        json={"email": alice.email, "name": "Impostor", "password": "another-pass-1"},
    )
    assert response.status_code == 409


def test_short_passwords_are_rejected(client):
    response = client.post(
        "/api/auth/register",
        json={"email": "weak@example.com", "name": "Weak", "password": "short"},
    )
    assert response.status_code == 422


def test_login_with_the_wrong_password_fails(client, alice):
    response = client.post(
        "/api/auth/login", json={"email": alice.email, "password": "not-the-password"}
    )
    assert response.status_code == 401


def test_login_does_not_reveal_whether_an_email_exists(client, alice):
    known = client.post(
        "/api/auth/login", json={"email": alice.email, "password": "wrong-password-x"}
    )
    unknown = client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "wrong-password-x"}
    )
    assert known.status_code == unknown.status_code == 401
    assert known.json()["detail"] == unknown.json()["detail"]


def test_password_is_never_returned(client, alice):
    assert "password" not in alice.get("/api/auth/me").text.lower()


def test_requests_without_a_token_are_rejected(client):
    assert client.get("/api/documents").status_code == 401


def test_a_garbage_token_is_rejected(client):
    response = client.get(
        "/api/documents", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert response.status_code == 401


def test_an_outsider_cannot_see_that_a_document_exists(alice, bob):
    document_id = alice.new_document("Private notes")
    # 404 rather than 403: a 403 would confirm the id is real.
    assert bob.get("/api/documents/%s" % document_id).status_code == 404


def test_a_viewer_can_read_but_not_write(alice, bob):
    document_id = alice.new_document("Shared", content="hello")
    alice.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": bob.email, "role": "VIEWER"},
    )
    assert bob.get("/api/documents/%s" % document_id).status_code == 200
    assert (
        bob.patch("/api/documents/%s" % document_id, json={"title": "Hijacked"}).status_code
        == 403
    )
    assert (
        bob.post(
            "/api/documents/%s/save" % document_id, json={"content": "overwritten"}
        ).status_code
        == 403
    )


def test_an_editor_can_write_but_not_manage_access(alice, bob, register):
    carol = register("Carol")
    document_id = alice.new_document("Shared", content="hello")
    alice.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": bob.email, "role": "EDITOR"},
    )
    assert (
        bob.patch("/api/documents/%s" % document_id, json={"title": "Renamed"}).status_code
        == 200
    )
    assert (
        bob.post(
            "/api/documents/%s/collaborators" % document_id,
            json={"email": carol.email, "role": "EDITOR"},
        ).status_code
        == 403
    )
    assert bob.delete("/api/documents/%s" % document_id).status_code == 403


def test_owner_can_change_and_revoke_a_role(alice, bob):
    document_id = alice.new_document("Shared")
    alice.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": bob.email, "role": "EDITOR"},
    )
    assert (
        alice.patch(
            "/api/documents/%s/collaborators/%s" % (document_id, bob.id),
            json={"role": "VIEWER"},
        ).json()["role"]
        == "VIEWER"
    )
    assert (
        alice.delete(
            "/api/documents/%s/collaborators/%s" % (document_id, bob.id)
        ).status_code
        == 204
    )
    assert bob.get("/api/documents/%s" % document_id).status_code == 404


def test_ownership_cannot_be_granted_through_the_collaborator_api(alice, bob):
    document_id = alice.new_document("Shared")
    response = alice.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": bob.email, "role": "OWNER"},
    )
    assert response.status_code == 400


def test_share_link_grants_access_and_can_be_revoked(alice, bob):
    document_id = alice.new_document("Link shared", content="hello")
    link = alice.put(
        "/api/documents/%s/share" % document_id, json={"role": "EDITOR"}
    ).json()
    assert link["share_token"]

    joined = bob.post("/api/documents/join/%s" % link["share_token"])
    assert joined.status_code == 200
    assert joined.json()["role"] == "EDITOR"
    assert bob.get("/api/documents/%s" % document_id).status_code == 200

    # Turning sharing off invalidates the link that was already handed out.
    alice.put("/api/documents/%s/share" % document_id, json={"role": None})
    assert bob.client.post(
        "/api/documents/join/%s" % link["share_token"], headers=bob.headers
    ).status_code == 404


def test_a_share_link_never_downgrades_an_existing_role(alice, bob):
    document_id = alice.new_document("Shared")
    alice.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": bob.email, "role": "EDITOR"},
    )
    link = alice.put(
        "/api/documents/%s/share" % document_id, json={"role": "VIEWER"}
    ).json()
    assert bob.post("/api/documents/join/%s" % link["share_token"]).json()["role"] == "EDITOR"


def test_only_the_owner_sees_the_share_token(alice, bob):
    document_id = alice.new_document("Shared")
    alice.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": bob.email, "role": "EDITOR"},
    )
    assert bob.get("/api/documents/%s" % document_id).json()["share_token"] is None
    assert bob.get("/api/documents/%s/share" % document_id).status_code == 403
