"""Backlinks, the page graph, the operation log, duplicate, lock, tasks, stats,
and comment reactions."""
from __future__ import annotations

from datetime import date, timedelta

from app.realtime.ot import TextOperation


def _share(owner, document_id, other, role="EDITOR"):
    response = owner.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": other.email, "role": role},
    )
    assert response.status_code == 201, response.text


# -- backlinks and graph ---------------------------------------------------------


def test_backlinks_list_the_pages_that_link_here(alice, bob):
    target = alice.new_document("Glossary")
    alice.new_document("Spec", "See the [Glossary](page:%s) for terms." % target)
    alice.new_document("Unrelated", "Nothing to see")
    hidden = bob.new_document("Bob's notes", "[x](page:%s)" % target)  # alice cannot see it
    assert hidden

    links = alice.get("/api/documents/%s/backlinks" % target).json()
    assert [link["title"] for link in links] == ["Spec"]
    assert links[0]["snippet"] == "See the Glossary for terms."


def test_the_graph_has_nesting_and_link_edges(alice):
    parent = alice.new_document("Hub", "one two three")
    child = alice.post("/api/documents", json={"title": "Leaf", "parent_id": parent}).json()["id"]
    other = alice.new_document("Other", "Back to [Hub](page:%s)" % parent)

    report = alice.get("/api/graph").json()
    nodes = {node["id"]: node for node in report["nodes"]}
    assert nodes[parent]["words"] == 3
    edges = {(e["source"], e["target"], e["kind"]) for e in report["edges"]}
    assert (parent, child, "parent") in edges
    assert (other, parent, "link") in edges


# -- operations (time-lapse) -------------------------------------------------------


def test_replaying_the_operation_log_reproduces_the_page(alice):
    document_id = alice.new_document("Story", "Once")
    for content in ("Once upon", "Once upon a time", "Once upon a time."):
        alice.post("/api/documents/%s/save" % document_id, json={"content": content})

    first = alice.get("/api/documents/%s/operations?limit=2" % document_id).json()
    assert len(first["operations"]) == 2 and first["next_after"] == 2
    assert first["names"][alice.id] == "Alice"
    rest = alice.get("/api/documents/%s/operations?after=%d" % (document_id, first["next_after"])).json()
    assert rest["next_after"] is None

    text = ""
    for op in first["operations"] + rest["operations"]:
        text = TextOperation.from_json(op["delta"]).apply(text)
    assert text == "Once upon a time."


def test_the_operation_log_needs_access(alice, bob):
    document_id = alice.new_document("Private", "secret")
    assert bob.get("/api/documents/%s/operations" % document_id).status_code == 404


# -- duplicate ------------------------------------------------------------------------


def test_duplicate_copies_content_and_subpages_and_relinks_them(alice, bob):
    root = alice.new_document("Handbook", "Start")
    child = alice.post("/api/documents", json={"title": "Chapter", "parent_id": root}).json()["id"]
    alice.post("/api/documents/%s/save" % root, json={"content": "Read [Chapter](page:%s)" % child})
    _share(alice, root, bob, "VIEWER")

    copy = bob.post("/api/documents/%s/duplicate" % root, json={"include_subpages": True})
    assert copy.status_code == 201, copy.text
    copy = copy.json()
    assert copy["title"] == "Handbook (copy)" and copy["role"] == "OWNER"

    pages = {d["id"]: d for d in bob.get("/api/documents").json()}
    copied_children = [d for d in pages.values() if d["parent_id"] == copy["id"]]
    assert [d["title"] for d in copied_children] == ["Chapter"]
    content = bob.get("/api/documents/%s" % copy["id"]).json()["content"]
    assert content == "Read [Chapter](page:%s)" % copied_children[0]["id"]


# -- lock -----------------------------------------------------------------------------


def test_a_locked_page_is_read_only_for_everyone_but_the_owner(alice, bob):
    document_id = alice.new_document("Policy", "v1")
    _share(alice, document_id, bob, "EDITOR")

    assert bob.put("/api/documents/%s/lock" % document_id, json={"locked": True}).status_code == 403
    locked = alice.put("/api/documents/%s/lock" % document_id, json={"locked": True})
    assert locked.status_code == 200 and locked.json()["locked"] is True

    assert bob.get("/api/documents/%s" % document_id).json()["role"] == "VIEWER"
    assert bob.post("/api/documents/%s/save" % document_id, json={"content": "v2"}).status_code == 403
    assert alice.post("/api/documents/%s/save" % document_id, json={"content": "v2"}).status_code == 200

    audit = alice.get("/api/documents/%s/audit" % document_id).json()
    assert audit[0]["action"] == "locked"
    alice.put("/api/documents/%s/lock" % document_id, json={"locked": False})
    assert bob.get("/api/documents/%s" % document_id).json()["role"] == "EDITOR"


# -- tasks and stats ----------------------------------------------------------------


def test_my_tasks_finds_assigned_and_own_todos_soonest_first(alice, bob):
    soon = (date.today() + timedelta(days=1)).isoformat()
    later = (date.today() + timedelta(days=9)).isoformat()
    content = "\n".join(
        [
            "- [ ] Write the brief @[Bob](user:%s) 📅 %s" % (bob.id, later),
            "- [ ] Book the room @[Bob](user:%s) 📅 %s" % (bob.id, soon),
            "- [x] Done already @[Bob](user:%s)" % bob.id,
            "- [ ] Alice's own thing",
            "- [ ] Someone else @[Alice](user:%s)" % alice.id,
        ]
    )
    document_id = alice.new_document("Launch", content)
    _share(alice, document_id, bob)

    bobs = bob.get("/api/me/tasks").json()
    assert [t["text"] for t in bobs] == ["Book the room @Bob", "Write the brief @Bob"]
    assert bobs[0]["due"] == soon and bobs[0]["line"] == 1 and bobs[0]["assigned"]

    alices = {t["text"]: t for t in alice.get("/api/me/tasks").json()}
    assert set(alices) == {"Alice's own thing", "Someone else @Alice"}
    assert alices["Alice's own thing"]["assigned"] is False


def test_stats_count_this_weeks_words(alice):
    alice.new_document("Essay", "word " * 50)  # 250 characters, about 50 words
    stats = alice.get("/api/me/stats").json()
    assert stats["words_this_week"] == 50
    assert len(stats["days"]) == 7 and stats["days"][-1]["words"] == 50
    assert stats["streak"] == 1 and stats["pages"] >= 1


# -- reactions ------------------------------------------------------------------------


def test_reactions_toggle_and_are_listed_with_comments(alice, bob):
    document_id = alice.new_document("Doc", "Some text here")
    _share(alice, document_id, bob, "VIEWER")
    comment = alice.post(
        "/api/documents/%s/comments" % document_id, json={"body": "Thoughts?", "anchor": "Some text"}
    ).json()
    url = "/api/documents/%s/comments/%s/reactions/" % (document_id, comment["id"])

    assert bob.put(url + "👍").json()["reactions"] == {"👍": [bob.id]}
    alice.put(url + "👍")
    alice.put(url + "🎉")
    bob.put(url + "👍")  # twice is a no-op
    listed = alice.get("/api/documents/%s/comments" % document_id).json()[0]
    assert listed["reactions"] == {"👍": [bob.id, alice.id], "🎉": [alice.id]}

    assert bob.delete(url + "👍").json()["reactions"] == {"👍": [alice.id], "🎉": [alice.id]}
    assert alice.put(url + "💩").status_code == 422
