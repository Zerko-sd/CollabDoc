"""The academic integrity report: effort, final-text share, and the flags."""
from __future__ import annotations

import random

import corpora


def _share(owner, document_id, other, role="EDITOR"):
    response = owner.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": other.email, "role": role},
    )
    assert response.status_code == 201, response.text


def _type(account, document_id, text, step=40):
    """Append ``text`` a few words at a time, the way typing reaches the server."""
    current = account.get("/api/documents/%s" % document_id).json()["content"]
    for end in range(step, len(text) + step, step):
        response = account.post(
            "/api/documents/%s/save" % document_id, json={"content": current + text[:end]}
        )
        assert response.status_code == 200, response.text
    return current + text


def _prime(account, document_id, style, seed):
    rng = random.Random(seed)
    for _ in range(6):
        response = account.post(
            "/api/documents/%s/attribution/samples" % document_id,
            json={"content": corpora.STYLES[style](rng, sentences=4)},
        )
        assert response.status_code == 201


def test_the_report_credits_each_person_with_what_they_typed(alice, bob):
    document_id = alice.new_document("Lab report")
    _share(alice, document_id, bob)
    _type(alice, document_id, "Alice wrote this opening line.\n")
    _type(bob, document_id, "Bob added a second line here.\n")

    report = alice.get("/api/documents/%s/integrity" % document_id).json()
    people = {c["user_id"]: c for c in report["contributors"]}
    assert set(people) == {alice.id, bob.id}
    assert people[alice.id]["surviving"] == len("Alice wrote this opening line.\n")
    assert people[bob.id]["surviving"] == len("Bob added a second line here.\n")
    assert people[alice.id]["pasted"] == 0 and people[alice.id]["typed"] > 0
    assert abs(sum(c["share"] for c in people.values()) - 1) < 0.01
    assert report["keystroke_share"] == 1.0
    assert people[bob.id]["sessions"] == 1
    assert not [f for f in report["flags"] if f["kind"] == "paste"]


def test_a_large_paste_is_flagged_where_it_sits(alice):
    document_id = alice.new_document("Essay")
    _type(alice, document_id, "My own intro.\n")
    pasted = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 4
    current = alice.get("/api/documents/%s" % document_id).json()["content"]
    alice.post("/api/documents/%s/save" % document_id, json={"content": current + pasted})

    report = alice.get("/api/documents/%s/integrity" % document_id).json()
    pastes = [f for f in report["flags"] if f["kind"] == "paste"]
    assert len(pastes) == 1
    flag = pastes[0]
    assert flag["user_id"] == alice.id and flag["severity"] == "medium"
    assert flag["start"] == len("My own intro.\n")
    assert report["keystroke_share"] < 0.2
    me = report["contributors"][0]
    assert me["pasted"] == len(pasted)


def test_a_paste_later_removed_is_kept_but_downgraded(alice):
    document_id = alice.new_document("Draft")
    pasted = "Borrowed paragraph that will not survive the edit. " * 4
    alice.post("/api/documents/%s/save" % document_id, json={"content": pasted})
    alice.post("/api/documents/%s/save" % document_id, json={"content": "Rewritten."})

    flag = next(
        f for f in alice.get("/api/documents/%s/integrity" % document_id).json()["flags"]
        if f["kind"] == "paste"
    )
    assert flag["severity"] == "low" and flag["start"] is None


def test_typing_in_someone_elses_style_is_flagged(alice, bob):
    document_id = alice.new_document("Group project")
    _share(alice, document_id, bob)
    _prime(alice, document_id, "academic", seed=11)
    _prime(bob, document_id, "blogger", seed=12)

    rng = random.Random(13)
    _type(alice, document_id, corpora.academic(rng, sentences=3) + "\n")
    _type(bob, document_id, corpora.blogger(rng, sentences=4) + "\n")
    # Bob types out a passage in Alice's register -- copied from her draft.
    copied = corpora.academic(rng, sentences=3)
    _type(bob, document_id, copied + "\n")
    report = alice.get("/api/documents/%s/integrity" % document_id).json()
    assert report["model"]["trained"]
    mismatches = [f for f in report["flags"] if f["kind"] == "style_mismatch"]
    assert len(mismatches) == 1, report["flags"]
    flag = mismatches[0]
    assert flag["user_id"] == bob.id and flag["other_user_id"] == alice.id
    assert flag["snippet"].startswith(copied[:30])
    assert 0 < report["agreement_rate"] < 1


def test_the_report_needs_access_to_the_page(alice, bob):
    document_id = alice.new_document("Private")
    assert bob.get("/api/documents/%s/integrity" % document_id).status_code in (403, 404)
    _share(alice, document_id, bob, role="VIEWER")
    assert bob.get("/api/documents/%s/integrity" % document_id).status_code == 200


def test_a_paste_in_someone_elses_style_is_one_strong_flag(alice, bob):
    document_id = alice.new_document("Group essay")
    _share(alice, document_id, bob)
    _prime(alice, document_id, "academic", seed=21)
    _prime(bob, document_id, "blogger", seed=22)
    rng = random.Random(23)
    _type(alice, document_id, corpora.academic(rng, sentences=3) + "\n")
    _type(bob, document_id, corpora.blogger(rng, sentences=4) + "\n")
    current = bob.get("/api/documents/%s" % document_id).json()["content"]
    bob.post(
        "/api/documents/%s/save" % document_id,
        json={"content": current + corpora.academic(rng, sentences=3) + "\n"},
    )

    flags = alice.get("/api/documents/%s/integrity" % document_id).json()["flags"]
    assert [f["kind"] for f in flags] == ["paste"], flags
    assert flags[0]["severity"] == "high"
    assert flags[0]["user_id"] == bob.id and flags[0]["other_user_id"] == alice.id
