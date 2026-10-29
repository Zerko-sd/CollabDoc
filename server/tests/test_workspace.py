"""Page icons, comments, search, and activity (the workspace features)."""
from __future__ import annotations

from app.db import SessionLocal
from app.models import DocumentBlock


def _share(owner, document_id, other, role="EDITOR"):
    response = owner.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": other.email, "role": role},
    )
    assert response.status_code == 201, response.text


# -- icons ------------------------------------------------------------------


def test_a_page_icon_is_set_listed_and_cleared(alice):
    created = alice.post("/api/documents", json={"title": "Roadmap", "icon": "🚀"})
    assert created.json()["icon"] == "🚀"
    document_id = created.json()["id"]

    renamed = alice.patch("/api/documents/%s" % document_id, json={"icon": "📌"})
    assert renamed.json()["icon"] == "📌"
    listed = {d["id"]: d["icon"] for d in alice.get("/api/documents").json()}
    assert listed[document_id] == "📌"

    # A title-only rename leaves the icon alone; an empty string clears it.
    alice.patch("/api/documents/%s" % document_id, json={"title": "Roadmap 2"})
    assert alice.get("/api/documents/%s" % document_id).json()["icon"] == "📌"
    cleared = alice.patch("/api/documents/%s" % document_id, json={"icon": ""})
    assert cleared.json()["icon"] is None


# -- comments ---------------------------------------------------------------


def test_threads_and_replies_round_trip(alice, bob):
    document_id = alice.new_document("Spec", "The cache is invalidated on write.\n")
    _share(alice, document_id, bob)

    thread = alice.post(
        "/api/documents/%s/comments" % document_id,
        json={"body": "Is this still true?", "anchor": "invalidated on write"},
    )
    assert thread.status_code == 201, thread.text
    thread = thread.json()
    assert thread["user"]["id"] == alice.id and thread["resolved"] is False

    reply = bob.post(
        "/api/documents/%s/comments" % document_id,
        json={"body": "Yes, since v2.", "parent_id": thread["id"]},
    ).json()
    # A reply inherits its thread's anchor.
    assert reply["anchor"] == "invalidated on write"
    assert reply["parent_id"] == thread["id"]

    nested = alice.post(
        "/api/documents/%s/comments" % document_id,
        json={"body": "nested", "parent_id": reply["id"]},
    )
    assert nested.status_code == 400

    listed = alice.get("/api/documents/%s/comments" % document_id).json()
    assert [c["id"] for c in listed] == [thread["id"], reply["id"]]


def test_viewers_can_comment_but_not_resolve_others_threads(alice, bob):
    document_id = alice.new_document("Spec")
    _share(alice, document_id, bob, role="VIEWER")

    alices = alice.post(
        "/api/documents/%s/comments" % document_id, json={"body": "Owner thread"}
    ).json()
    bobs = bob.post("/api/documents/%s/comments" % document_id, json={"body": "Viewer thread"})
    assert bobs.status_code == 201
    bobs = bobs.json()

    url = "/api/documents/%s/comments/%s"
    assert bob.patch(url % (document_id, alices["id"]), json={"resolved": True}).status_code == 403
    assert bob.patch(url % (document_id, bobs["id"]), json={"resolved": True}).json()["resolved"]
    # The owner can resolve anyone's thread and delete it; a viewer cannot delete the owner's.
    assert alice.patch(url % (document_id, bobs["id"]), json={"resolved": False}).status_code == 200
    assert bob.delete(url % (document_id, alices["id"])).status_code == 403
    assert bob.patch(url % (document_id, alices["id"]), json={"body": "edited"}).status_code == 403
    assert alice.delete(url % (document_id, bobs["id"])).status_code == 204
    assert [c["id"] for c in alice.get("/api/documents/%s/comments" % document_id).json()] == [
        alices["id"]
    ]


def test_deleting_a_thread_removes_its_replies(alice):
    document_id = alice.new_document("Spec")
    url = "/api/documents/%s/comments" % document_id
    thread = alice.post(url, json={"body": "Thread"}).json()
    alice.post(url, json={"body": "Reply", "parent_id": thread["id"]})
    assert alice.delete("%s/%s" % (url, thread["id"])).status_code == 204
    assert alice.get(url).json() == []


def test_comments_are_private_to_people_with_access(alice, bob):
    document_id = alice.new_document("Private")
    url = "/api/documents/%s/comments" % document_id
    assert bob.get(url).status_code == 404
    assert bob.post(url, json={"body": "hello"}).status_code == 404


# -- search -----------------------------------------------------------------


def test_search_matches_titles_and_text_it_is_allowed_to_see(alice, bob):
    by_title = alice.new_document("Zebra migration plan")
    by_text = alice.new_document("Notes")
    hidden = bob.new_document("Zebra secrets")
    # The block table is filled by the attribution pass; seed it directly so
    # the test does not depend on that worker's timing.
    with SessionLocal() as db:
        db.add(DocumentBlock(document_id=by_text, content="Feed the zebra at noon", sort_order=1))
        db.add(DocumentBlock(document_id=hidden, content="zebra zebra", sort_order=1))
        db.commit()

    hits = alice.get("/api/search", params={"q": "zebra"}).json()
    ids = [hit["document_id"] for hit in hits]
    assert by_title in ids and by_text in ids
    assert hidden not in ids
    # A title match ranks ahead of a body match.
    assert ids.index(by_title) < ids.index(by_text)
    snippet = next(hit["snippet"] for hit in hits if hit["document_id"] == by_text)
    assert "zebra" in snippet


def test_search_treats_wildcards_literally(alice):
    alice.new_document("100% done")
    alice.new_document("Nothing here")
    titles = [hit["title"] for hit in alice.get("/api/search", params={"q": "%"}).json()]
    assert titles == ["100% done"]


# -- activity ---------------------------------------------------------------


def test_activity_summarises_the_operation_log_per_author(alice, bob):
    document_id = alice.new_document("Log", "Hello")
    _share(alice, document_id, bob)
    bob.post("/api/documents/%s/save" % document_id, json={"content": "Hello there"})

    report = alice.get("/api/documents/%s/activity" % document_id).json()
    authors = {a["user_id"]: a for a in report["authors"]}
    assert authors[alice.id]["inserted"] == 5
    assert authors[bob.id]["inserted"] == 6
    assert authors[bob.id]["name"] == "Bob"
    # The chart measures characters changed, not operations (keystrokes).
    assert sum(sum(b["counts"].values()) for b in report["buckets"]) == 11
    assert authors[alice.id]["sessions"] == authors[bob.id]["sessions"] == 1
    assert report["first_edit"] is not None


def test_activity_groups_rapid_edits_into_one_session(alice):
    document_id = alice.new_document("Draft", "a")
    for content in ("ab", "abc", "abcd", "abcde"):
        alice.post("/api/documents/%s/save" % document_id, json={"content": content})

    report = alice.get("/api/documents/%s/activity" % document_id).json()
    (author,) = report["authors"]
    assert author["operations"] == 5
    assert author["sessions"] == 1 and author["active_seconds"] >= 30
    (session,) = report["sessions"]
    assert session["name"] == "Alice" and session["inserted"] == 5


def test_an_idle_pause_splits_activity_sessions_newest_first(alice, bob, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "version_group_idle_seconds", -1.0)
    document_id = alice.new_document("Notes", "one")
    _share(alice, document_id, bob)
    bob.post("/api/documents/%s/save" % document_id, json={"content": "one two"})

    report = alice.get("/api/documents/%s/activity" % document_id).json()
    assert [s["name"] for s in report["sessions"]] == ["Bob", "Alice"]
    assert report["sessions"][0]["inserted"] == 4


def test_activity_of_an_untouched_page_is_empty(alice):
    document_id = alice.new_document("Blank")
    report = alice.get("/api/documents/%s/activity" % document_id).json()
    assert report["buckets"] == [] and report["authors"] == []
