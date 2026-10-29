"""Mentions, reply and invite notifications, and their live delivery."""
from __future__ import annotations

from app.notifications import mentioned_ids, plain


def mention(account) -> str:
    return "@[%s](user:%s)" % (account.name, account.id)


def share(owner, document_id, guest, role="EDITOR"):
    owner.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": guest.email, "role": role},
    )


def inbox(account, **params) -> list[dict]:
    response = account.get("/api/notifications", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_mention_syntax_is_parsed_and_flattened(alice, bob):
    text = "Ask %s and %s, then %s again" % (mention(alice), mention(bob), mention(alice))
    assert mentioned_ids(text) == [alice.id, bob.id]
    assert plain(text) == "Ask @Alice and @Bob, then @Alice again"


def test_being_invited_is_a_notification(alice, bob):
    document_id = alice.new_document("Plans")
    share(alice, document_id, bob)
    [note] = inbox(bob)
    assert note["kind"] == "SHARED"
    assert note["document_id"] == document_id
    assert note["document_title"] == "Plans"
    assert note["actor"]["id"] == alice.id
    assert note["read"] is False


def test_mentioning_someone_in_the_page(alice, bob):
    document_id = alice.new_document("Plans")
    share(alice, document_id, bob)
    response = alice.post(
        "/api/documents/%s/mentions" % document_id,
        json={"user_id": bob.id, "snippet": "Over to %s for the budget" % mention(bob)},
    )
    assert response.status_code == 201, response.text
    note = inbox(bob)[0]
    assert note["kind"] == "MENTION"
    assert note["snippet"] == "Over to @Bob for the budget"


def test_mentioning_someone_without_access_is_refused(alice, bob):
    document_id = alice.new_document("Private")
    response = alice.post(
        "/api/documents/%s/mentions" % document_id, json={"user_id": bob.id}
    )
    assert response.status_code == 400
    assert inbox(bob) == []


def test_mentioning_yourself_is_silent(alice):
    document_id = alice.new_document("Notes")
    response = alice.post("/api/documents/%s/mentions" % document_id, json={"user_id": alice.id})
    assert response.status_code == 201
    assert inbox(alice) == []


def test_viewers_cannot_mention_through_the_page(alice, bob, register):
    carol = register("Carol")
    document_id = alice.new_document("Plans")
    share(alice, document_id, bob, "VIEWER")
    share(alice, document_id, carol)
    response = bob.post("/api/documents/%s/mentions" % document_id, json={"user_id": carol.id})
    assert response.status_code == 403


def test_comment_mentions_and_replies_notify_once(alice, bob, register):
    carol = register("Carol")
    document_id = alice.new_document("Plans", content="The plan")
    share(alice, document_id, bob)
    share(alice, document_id, carol)

    thread = bob.post(
        "/api/documents/%s/comments" % document_id,
        json={"body": "What do you think %s?" % mention(carol), "anchor": "plan"},
    ).json()
    [note] = [n for n in inbox(carol) if n["kind"] == "MENTION"]
    assert note["comment_id"] == thread["id"]

    # Alice replies and mentions Bob, who also wrote the thread: one notification.
    alice.post(
        "/api/documents/%s/comments" % document_id,
        json={"body": "Agreed, %s" % mention(bob), "parent_id": thread["id"]},
    )
    about_reply = [n for n in inbox(bob) if n["kind"] != "SHARED"]
    assert [n["kind"] for n in about_reply] == ["MENTION"]

    # Carol replies without a mention: Bob hears about it as a reply.
    carol.post(
        "/api/documents/%s/comments" % document_id,
        json={"body": "Fine by me", "parent_id": thread["id"]},
    )
    assert inbox(bob)[0]["kind"] == "COMMENT_REPLY"


def test_marking_notifications_read(alice, bob):
    for title in ("One", "Two"):
        share(alice, alice.new_document(title), bob)
    first, second = inbox(bob)

    bob.post("/api/notifications/read", json={"ids": [first["id"]]})
    assert [n["id"] for n in inbox(bob, unread=True)] == [second["id"]]
    bob.post("/api/notifications/read", json={})
    assert inbox(bob, unread=True) == []
    # Nobody else's inbox is touched.
    assert alice.post("/api/notifications/read", json={"ids": [second["id"]]}).status_code == 204


def test_notifications_about_trashed_pages_are_hidden(alice, bob):
    document_id = alice.new_document("Soon gone")
    share(alice, document_id, bob)
    alice.delete("/api/documents/%s" % document_id)
    assert inbox(bob) == []


def test_notifications_arrive_live_on_the_user_socket(alice, bob):
    document_id = alice.new_document("Live")
    with bob.client.websocket_connect("/ws/me?token=%s" % bob.token) as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        share(alice, document_id, bob)
        message = ws.receive_json()
    assert message["type"] == "notification"
    assert message["notification"]["kind"] == "SHARED"
    assert message["notification"]["document_id"] == document_id


def test_the_user_socket_needs_a_valid_token(client):
    from starlette.websockets import WebSocketDisconnect

    try:
        with client.websocket_connect("/ws/me?token=nonsense") as ws:
            ws.receive_json()
    except WebSocketDisconnect as exc:
        assert exc.code == 4401
    else:  # pragma: no cover
        raise AssertionError("the socket should have been refused")
