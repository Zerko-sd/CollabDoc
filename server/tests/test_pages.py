"""Nested pages, the trash, favourites and publishing to the web."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import Document
from app.routers.documents import purge_trash


def new_page(account, title, parent_id=None, content=""):
    response = account.post(
        "/api/documents", json={"title": title, "content": content, "parent_id": parent_id}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def listed(account) -> dict[str, dict]:
    return {doc["id"]: doc for doc in account.get("/api/documents").json()}


def share(owner, document_id, guest, role="EDITOR"):
    response = owner.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": guest.email, "role": role},
    )
    assert response.status_code == 201, response.text


# -- nesting ----------------------------------------------------------------


def test_a_sub_page_records_its_parent(alice):
    parent = new_page(alice, "Parent")
    child = new_page(alice, "Child", parent)
    pages = listed(alice)
    assert pages[child]["parent_id"] == parent
    assert pages[parent]["parent_id"] is None


def test_sharing_a_page_shares_its_sub_pages(alice, bob):
    parent = new_page(alice, "Team space")
    child = new_page(alice, "Meeting notes", parent)
    share(alice, parent, bob, "VIEWER")

    pages = listed(bob)
    assert pages[child]["parent_id"] == parent
    assert pages[child]["role"] == "VIEWER"
    assert bob.get("/api/documents/%s" % child).status_code == 200
    # Inherited access is still only what the parent grant allows.
    assert bob.post("/api/documents/%s/save" % child, json={"content": "x"}).status_code == 403


def test_owning_a_parent_does_not_make_you_owner_of_a_guest_sub_page(alice, bob):
    parent = new_page(alice, "Alice's page")
    share(alice, parent, bob, "EDITOR")
    bobs = new_page(bob, "Bob's sub-page", parent)
    assert listed(alice)[bobs]["role"] == "EDITOR"
    assert alice.delete("/api/documents/%s" % bobs).status_code == 403


def test_people_with_access_through_a_parent_are_listed_as_inherited(alice, bob):
    parent = new_page(alice, "Team space")
    child = new_page(alice, "Notes", parent)
    share(alice, parent, bob, "VIEWER")
    people = {
        entry["user"]["id"]: entry
        for entry in alice.get("/api/documents/%s/collaborators" % child).json()
    }
    assert people[alice.id]["role"] == "OWNER" and not people[alice.id]["inherited"]
    assert people[bob.id]["role"] == "VIEWER" and people[bob.id]["inherited"]


def test_viewers_cannot_add_sub_pages(alice, bob):
    parent = new_page(alice, "Read only")
    share(alice, parent, bob, "VIEWER")
    response = bob.post("/api/documents", json={"title": "Sneaky", "parent_id": parent})
    assert response.status_code == 403


def test_a_page_whose_parent_you_cannot_see_is_listed_at_the_top(alice, bob):
    parent = new_page(alice, "Private")
    child = new_page(alice, "Shared child", parent)
    share(alice, child, bob)
    assert listed(bob)[child]["parent_id"] is None


def test_moving_a_page(alice):
    first = new_page(alice, "First")
    second = new_page(alice, "Second")
    child = new_page(alice, "Child", first)

    assert alice.patch("/api/documents/%s" % child, json={"parent_id": second}).status_code == 200
    assert listed(alice)[child]["parent_id"] == second
    # Renaming without parent_id leaves it in place.
    alice.patch("/api/documents/%s" % child, json={"title": "Renamed"})
    assert listed(alice)[child]["parent_id"] == second
    # An explicit null moves it to the top level.
    alice.patch("/api/documents/%s" % child, json={"parent_id": None})
    assert listed(alice)[child]["parent_id"] is None


def test_a_page_cannot_be_moved_inside_itself(alice):
    top = new_page(alice, "Top")
    middle = new_page(alice, "Middle", top)
    bottom = new_page(alice, "Bottom", middle)
    for target in (top, bottom):
        response = alice.patch("/api/documents/%s" % top, json={"parent_id": target})
        assert response.status_code == 400


# -- trash ------------------------------------------------------------------


def test_deleting_moves_a_page_and_its_sub_pages_to_the_trash(alice):
    parent = new_page(alice, "Parent", content="keep me")
    child = new_page(alice, "Child", parent)
    assert alice.delete("/api/documents/%s" % parent).status_code == 204

    assert parent not in listed(alice) and child not in listed(alice)
    assert alice.get("/api/documents/%s" % child).status_code == 404
    trash = [doc["id"] for doc in alice.get("/api/documents/trash").json()]
    assert trash == [parent]  # the child is restored with it, not listed apart

    restored = alice.post("/api/documents/%s/restore" % parent)
    assert restored.status_code == 200
    assert listed(alice)[child]["parent_id"] == parent
    assert alice.get("/api/documents/%s" % parent).json()["content"] == "keep me"


def test_restoring_a_page_whose_parent_is_still_trashed_brings_it_to_the_top(alice):
    parent = new_page(alice, "Parent")
    child = new_page(alice, "Child", parent)
    alice.delete("/api/documents/%s" % child)
    alice.delete("/api/documents/%s" % parent)
    alice.post("/api/documents/%s/restore" % child)
    assert listed(alice)[child]["parent_id"] is None


def test_only_the_owner_can_trash_or_restore(alice, bob):
    page = new_page(alice, "Mine")
    share(alice, page, bob)
    assert bob.delete("/api/documents/%s" % page).status_code == 403
    alice.delete("/api/documents/%s" % page)
    assert bob.post("/api/documents/%s/restore" % page).status_code == 404
    assert page not in [doc["id"] for doc in bob.get("/api/documents/trash").json()]


def test_permanent_delete(alice):
    parent = new_page(alice, "Gone")
    child = new_page(alice, "Also gone", parent)
    alice.delete("/api/documents/%s" % parent)
    assert alice.delete("/api/documents/%s?permanent=true" % parent).status_code == 204
    assert alice.post("/api/documents/%s/restore" % child).status_code == 404
    assert alice.get("/api/documents/trash").json() == []


def test_the_trash_is_emptied_after_thirty_days(alice):
    old = new_page(alice, "Old")
    recent = new_page(alice, "Recent")
    alice.delete("/api/documents/%s" % old)
    alice.delete("/api/documents/%s" % recent)
    with SessionLocal() as db:
        db.get(Document, old).deleted_at = datetime.now(timezone.utc) - timedelta(days=31)
        db.commit()
        assert purge_trash(db) == 1
        assert db.get(Document, old) is None
        assert db.get(Document, recent) is not None


def test_search_skips_the_trash_and_finds_shared_sub_pages(alice, bob):
    parent = new_page(alice, "Handbook")
    child = new_page(alice, "Zebra onboarding", parent)
    share(alice, parent, bob)
    hits = [hit["document_id"] for hit in bob.get("/api/search", params={"q": "zebra"}).json()]
    assert hits == [child]
    alice.delete("/api/documents/%s" % child)
    assert bob.get("/api/search", params={"q": "zebra"}).json() == []


# -- favourites -------------------------------------------------------------


def test_favourites_follow_the_user_and_keep_their_order(alice, bob):
    first = new_page(alice, "First")
    second = new_page(alice, "Second")
    secret = new_page(bob, "Not yours")
    saved = alice.put(
        "/api/me/favorites", json={"document_ids": [second, first, secret, second]}
    ).json()
    assert saved == [second, first]
    assert alice.get("/api/me/favorites").json() == [second, first]
    alice.delete("/api/documents/%s" % second)
    assert alice.get("/api/me/favorites").json() == [first]


# -- publishing -------------------------------------------------------------


def test_publishing_serves_a_read_only_page_without_sign_in(alice, client):
    page = new_page(alice, "Launch notes", content="# Hello\n- **bold** item\n<script>x</script>")
    assert client.get("/p/%s" % page).status_code == 404

    published = alice.put("/api/documents/%s/publish" % page, json={"published": True}).json()
    assert published["url"].endswith("/p/%s" % page)
    response = client.get("/p/%s" % page)
    assert response.status_code == 200
    assert "<h1>Hello</h1>" in response.text
    assert "<strong>bold</strong>" in response.text
    assert "<script>" not in response.text
    assert "default-src 'none'" in response.headers["content-security-policy"]

    alice.put("/api/documents/%s/publish" % page, json={"published": False})
    assert client.get("/p/%s" % page).status_code == 404


def test_only_the_owner_can_publish(alice, bob):
    page = new_page(alice, "Draft")
    share(alice, page, bob)
    assert bob.put("/api/documents/%s/publish" % page, json={"published": True}).status_code == 403


def test_a_trashed_page_is_no_longer_published(alice, client):
    page = new_page(alice, "Public")
    alice.put("/api/documents/%s/publish" % page, json={"published": True})
    alice.delete("/api/documents/%s" % page)
    assert client.get("/p/%s" % page).status_code == 404
