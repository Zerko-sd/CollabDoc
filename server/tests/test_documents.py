"""Document CRUD, saving, and version history (WBS 9.1, covers WBS 4.1/4.2)."""
from __future__ import annotations


def test_create_and_fetch_a_document(alice):
    response = alice.post(
        "/api/documents", json={"title": "Research notes", "content": "First line.\n"}
    )
    assert response.status_code == 201
    document_id = response.json()["id"]

    detail = alice.get("/api/documents/%s" % document_id).json()
    assert detail["title"] == "Research notes"
    assert detail["content"] == "First line.\n"
    assert detail["role"] == "OWNER"
    # Seed text is recorded as an operation, so the document starts at v1.
    assert detail["current_version"] == 1


def test_an_empty_document_starts_at_version_zero(alice):
    document_id = alice.new_document("Blank")
    detail = alice.get("/api/documents/%s" % document_id).json()
    assert detail["current_version"] == 0
    assert detail["content"] == ""


def test_listing_covers_owned_and_shared_documents(alice, bob):
    mine = alice.new_document("Mine")
    shared = alice.new_document("Shared")
    alice.post(
        "/api/documents/%s/collaborators" % shared,
        json={"email": bob.email, "role": "EDITOR"},
    )

    titles = {d["id"]: d["role"] for d in alice.get("/api/documents").json()}
    assert titles[mine] == "OWNER" and titles[shared] == "OWNER"

    bob_documents = bob.get("/api/documents").json()
    assert [d["id"] for d in bob_documents] == [shared]
    assert bob_documents[0]["role"] == "EDITOR"


def test_manual_save_records_an_operation(alice):
    document_id = alice.new_document("Draft")
    saved = alice.post(
        "/api/documents/%s/save" % document_id,
        json={"content": "The first draft.", "label": "first cut"},
    ).json()
    assert saved["content"] == "The first draft."
    assert saved["current_version"] == 1

    snapshots = alice.get("/api/documents/%s/snapshots" % document_id).json()
    assert snapshots[0]["label"] == "first cut"


def test_saving_the_same_content_twice_adds_no_operation(alice):
    document_id = alice.new_document("Draft", content="Stable text.")
    alice.post("/api/documents/%s/save" % document_id, json={"content": "Stable text."})
    versions = alice.get("/api/documents/%s/versions" % document_id).json()
    assert len(versions) == 1


def test_version_history_reconstructs_earlier_states(alice):
    document_id = alice.new_document("Essay", content="one")
    for text in ("one two", "one two three", "one two three four"):
        alice.post("/api/documents/%s/save" % document_id, json={"content": text})

    # One author typing without a pause is one entry in the history ...
    versions = alice.get("/api/documents/%s/versions" % document_id).json()
    assert len(versions) == 1
    assert versions[0]["number"] == 1
    assert versions[0]["version"] == 4
    assert versions[0]["first_version"] == 1
    assert versions[0]["operation_count"] == 4
    assert versions[0]["user_name"] == "Alice"
    assert versions[0]["delta"] == ["one two three four"]

    # ... but every operation stays individually restorable.

    for version, expected in enumerate(
        ["", "one", "one two", "one two three", "one two three four"]
    ):
        got = alice.get("/api/documents/%s/versions/%d" % (document_id, version)).json()
        assert got["content"] == expected


def test_requesting_a_version_that_does_not_exist(alice):
    document_id = alice.new_document("Essay", content="one")
    assert alice.get("/api/documents/%s/versions/99" % document_id).status_code == 404


def test_revert_appends_rather_than_rewriting_history(alice):
    document_id = alice.new_document("Essay", content="first")
    alice.post("/api/documents/%s/save" % document_id, json={"content": "first second"})
    alice.post(
        "/api/documents/%s/save" % document_id, json={"content": "first second third"}
    )

    reverted = alice.post(
        "/api/documents/%s/revert" % document_id, json={"version": 1}
    ).json()
    assert reverted["content"] == "first"
    # The rollback is itself version 4 -- the log is append-only.
    assert reverted["current_version"] == 4
    assert (
        alice.get("/api/documents/%s/versions/3" % document_id).json()["content"]
        == "first second third"
    )


def test_reverting_to_an_unknown_version_is_a_404(alice):
    document_id = alice.new_document("Essay", content="first")
    assert (
        alice.post("/api/documents/%s/revert" % document_id, json={"version": 12}).status_code
        == 404
    )


def test_rename(alice):
    document_id = alice.new_document("Working title")
    assert (
        alice.patch("/api/documents/%s" % document_id, json={"title": "Final title"}).json()[
            "title"
        ]
        == "Final title"
    )


def test_delete_removes_the_document_and_its_history(alice):
    document_id = alice.new_document("Temporary", content="text")
    assert alice.delete("/api/documents/%s" % document_id).status_code == 204
    assert alice.get("/api/documents/%s" % document_id).status_code == 404


def test_collaborator_count_includes_the_owner(alice, bob):
    document_id = alice.new_document("Shared")
    alice.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": bob.email, "role": "VIEWER"},
    )
    assert alice.get("/api/documents/%s" % document_id).json()["collaborator_count"] == 2


def test_a_labelled_save_closes_the_burst(alice):
    document_id = alice.new_document("Essay", content="one")
    alice.post(
        "/api/documents/%s/save" % document_id,
        json={"content": "one two", "label": "draft"},
    )
    alice.post("/api/documents/%s/save" % document_id, json={"content": "one two three"})

    versions = alice.get("/api/documents/%s/versions" % document_id).json()
    assert [(v["first_version"], v["version"]) for v in versions] == [(3, 3), (1, 2)]
    assert versions[1]["label"] == "draft"


def test_a_different_author_starts_a_new_entry(alice, bob):
    document_id = alice.new_document("Essay", content="one")
    alice.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": bob.email, "role": "EDITOR"},
    )
    bob.post("/api/documents/%s/save" % document_id, json={"content": "one two"})
    alice.post("/api/documents/%s/save" % document_id, json={"content": "one two three"})

    versions = alice.get("/api/documents/%s/versions" % document_id).json()
    assert [v["user_name"] for v in versions] == ["Alice", "Bob", "Alice"]


def test_an_idle_pause_starts_a_new_entry(alice, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "version_group_idle_seconds", -1.0)
    document_id = alice.new_document("Essay", content="one")
    alice.post("/api/documents/%s/save" % document_id, json={"content": "one two"})

    versions = alice.get("/api/documents/%s/versions" % document_id).json()
    assert [v["version"] for v in versions] == [2, 1]
    assert [v["number"] for v in versions] == [2, 1]
    assert len(alice.get("/api/documents/%s/versions?limit=1" % document_id).json()) == 1
