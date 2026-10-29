"""End-to-end collaborative editing over WebSockets (WBS 9.2, covers WBS 5.x)."""
from __future__ import annotations

import pytest


def inbox(ws) -> list[dict]:
    """Messages that arrived while a test was waiting for a different kind."""
    if not hasattr(ws, "_inbox"):
        ws._inbox = []
    return ws._inbox


def receive_until(ws, kind: str, limit: int = 20) -> dict:
    """Next message of ``kind``, setting anything else aside.

    Presence and attribution updates arrive on their own schedule, so a test
    waiting for an operation cannot assume the next frame is the one it wants
    -- and must not throw away the frames it skipped, which may be exactly
    what the next assertion is about.
    """
    buffered = inbox(ws)
    for index, message in enumerate(buffered):
        if message["type"] == kind:
            return buffered.pop(index)
    for _ in range(limit):
        message = ws.receive_json()
        if message["type"] == kind:
            return message
        buffered.append(message)
    raise AssertionError("no %r message arrived" % kind)


def socket(account, document_id):
    return account.client.websocket_connect(
        "/ws/documents/%s?token=%s" % (document_id, account.token)
    )


def test_init_carries_the_document_state(alice):
    document_id = alice.new_document("Live", content="hello world")
    with socket(alice, document_id) as ws:
        init = ws.receive_json()
        assert init["type"] == "init"
        assert init["content"] == "hello world"
        assert init["version"] == 1
        assert init["you"]["user_id"] == alice.id
        assert init["you"]["can_edit"] is True


def test_an_edit_is_acknowledged_and_broadcast(alice, bob):
    document_id = alice.new_document("Live", content="hello")
    alice.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": bob.email, "role": "EDITOR"},
    )
    with socket(alice, document_id) as a, socket(bob, document_id) as b:
        a.receive_json()
        b.receive_json()

        a.send_json({"type": "op", "version": 1, "operation": [5, " world"]})
        ack = receive_until(a, "ack")
        assert ack["version"] == 2

        broadcast = receive_until(b, "op")
        assert broadcast["operation"] == [5, " world"]
        assert broadcast["user_id"] == alice.id

    assert alice.get("/api/documents/%s" % document_id).json()["content"] == "hello world"


def test_the_sender_does_not_receive_its_own_edit(alice, bob):
    document_id = alice.new_document("Live", content="hello")
    alice.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": bob.email, "role": "EDITOR"},
    )
    with socket(alice, document_id) as a, socket(bob, document_id) as b:
        a.receive_json()
        b.receive_json()
        a.send_json({"type": "op", "version": 1, "operation": [5, "!"]})
        assert receive_until(a, "ack")["version"] == 2
        a.send_json({"type": "ping"})
        receive_until(a, "pong")
        # Anything that arrived before the pong would be an echo of Alice's
        # own edit coming back to her.
        assert [m["type"] for m in inbox(a) if m["type"] == "op"] == []


def test_concurrent_edits_at_the_same_version_converge(alice, bob):
    document_id = alice.new_document("Live", content="hello")
    alice.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": bob.email, "role": "EDITOR"},
    )
    with socket(alice, document_id) as a, socket(bob, document_id) as b:
        a.receive_json()
        b.receive_json()

        # Both write against version 1 without seeing each other's edit.
        a.send_json({"type": "op", "version": 1, "operation": ["Alice: ", 5]})
        b.send_json({"type": "op", "version": 1, "operation": [5, " from Bob"]})

        assert receive_until(a, "ack")["version"] in (2, 3)
        assert receive_until(b, "ack")["version"] in (2, 3)
        # Each side is told about the other's edit, already rebased.
        receive_until(a, "op")
        receive_until(b, "op")

    content = alice.get("/api/documents/%s" % document_id).json()["content"]
    assert content == "Alice: hello from Bob"


def test_an_edit_written_against_a_stale_version_is_rebased(alice, bob):
    document_id = alice.new_document("Live", content="abcdef")
    alice.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": bob.email, "role": "EDITOR"},
    )
    with socket(alice, document_id) as a, socket(bob, document_id) as b:
        a.receive_json()
        b.receive_json()

        a.send_json({"type": "op", "version": 1, "operation": [3, "XYZ", 3]})
        receive_until(a, "ack")
        receive_until(b, "op")

        # Bob still believes he is on version 1 and appends at the old offset.
        b.send_json({"type": "op", "version": 1, "operation": [6, "!"]})
        receive_until(b, "ack")

    assert alice.get("/api/documents/%s" % document_id).json()["content"] == "abcXYZdef!"


def test_a_viewer_is_refused_the_right_to_edit(alice, bob):
    document_id = alice.new_document("Live", content="hello")
    alice.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": bob.email, "role": "VIEWER"},
    )
    with socket(bob, document_id) as b:
        init = b.receive_json()
        assert init["you"]["can_edit"] is False
        b.send_json({"type": "op", "version": 1, "operation": [5, " nope"]})
        error = receive_until(b, "error")
        assert error["code"] == "forbidden"

    assert alice.get("/api/documents/%s" % document_id).json()["content"] == "hello"


def test_presence_lists_everyone_in_the_room(alice, bob):
    document_id = alice.new_document("Live")
    alice.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": bob.email, "role": "EDITOR"},
    )
    with socket(alice, document_id) as a:
        a.receive_json()
        receive_until(a, "presence")
        with socket(bob, document_id) as b:
            b.receive_json()
            joined = receive_until(a, "presence")
            assert {u["user_id"] for u in joined["users"]} == {alice.id, bob.id}
            assert all(u["presence_color"].startswith("#") for u in joined["users"])
        left = receive_until(a, "presence")
        assert [u["user_id"] for u in left["users"]] == [alice.id]


def test_a_remote_cursor_is_carried_over_later_edits(alice, bob):
    document_id = alice.new_document("Live", content="hello world")
    alice.post(
        "/api/documents/%s/collaborators" % document_id,
        json={"email": bob.email, "role": "EDITOR"},
    )
    with socket(alice, document_id) as a, socket(bob, document_id) as b:
        a.receive_json()
        b.receive_json()

        a.send_json({"type": "op", "version": 1, "operation": ["XXXXX", 11]})
        receive_until(a, "ack")
        receive_until(b, "op")

        # Bob's caret was at offset 6 back when the document was "hello world".
        b.send_json({"type": "cursor", "version": 1, "position": 6})
        moved = receive_until(a, "cursor")
        assert moved["position"] == 11
        assert moved["user_id"] == bob.id


def test_an_unauthenticated_socket_is_closed(alice):
    document_id = alice.new_document("Live")
    from starlette.testclient import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as caught:
        with alice.client.websocket_connect(
            "/ws/documents/%s?token=rubbish" % document_id
        ) as ws:
            ws.receive_json()
    assert caught.value.code == 4401


def test_a_stranger_is_closed_out_of_the_room(alice, bob):
    document_id = alice.new_document("Private")
    from starlette.testclient import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as caught:
        with socket(bob, document_id) as ws:
            ws.receive_json()
    assert caught.value.code == 4403


def test_malformed_frames_are_reported_not_fatal(alice):
    document_id = alice.new_document("Live", content="hello")
    with socket(alice, document_id) as ws:
        ws.receive_json()
        ws.send_text("{not json")
        assert receive_until(ws, "error")["code"] == "bad_json"
        ws.send_json({"type": "op", "version": 1, "operation": "not-a-list"})
        assert receive_until(ws, "error")["code"] == "bad_operation"
        # The socket still works afterwards.
        ws.send_json({"type": "op", "version": 1, "operation": [5, "!"]})
        assert receive_until(ws, "ack")["version"] == 2


def test_save_over_the_socket_pins_a_snapshot(alice):
    document_id = alice.new_document("Live", content="hello")
    with socket(alice, document_id) as ws:
        ws.receive_json()
        ws.send_json({"type": "save", "label": "checkpoint"})
        assert receive_until(ws, "saved")["version"] == 1
    labels = [s["label"] for s in alice.get("/api/documents/%s/snapshots" % document_id).json()]
    assert "checkpoint" in labels


def test_edits_survive_everyone_disconnecting(alice):
    document_id = alice.new_document("Live", content="hello")
    with socket(alice, document_id) as ws:
        ws.receive_json()
        ws.send_json({"type": "op", "version": 1, "operation": [5, " again"]})
        receive_until(ws, "ack")
    # The room is torn down here, which flushes the queued operation.
    assert alice.get("/api/documents/%s" % document_id).json()["content"] == "hello again"
    versions = alice.get("/api/documents/%s/versions" % document_id).json()
    assert [v["operation_count"] for v in versions] == [2]


def test_version_number_holds_while_one_person_keeps_typing(alice):
    document_id = alice.new_document("Live", content="hello")
    with socket(alice, document_id) as ws:
        init = ws.receive_json()
        assert init["version_number"] == 1
        revision = init["version"]
        for index, letter in enumerate("world"):
            ws.send_json(
                {"type": "op", "version": revision, "operation": [5 + index, letter]}
            )
            ack = receive_until(ws, "ack")
            revision = ack["version"]
            # The OT revision moves on every keystroke; the version does not.
            assert ack["version"] == 2 + index
            assert ack["version_number"] == 1

        ws.send_json({"type": "save", "label": "checkpoint"})
        assert receive_until(ws, "saved")["version_number"] == 1
        ws.send_json({"type": "op", "version": revision, "operation": [10, "!"]})
        assert receive_until(ws, "ack")["version_number"] == 2

    detail = alice.get("/api/documents/%s" % document_id).json()
    assert detail["version_number"] == 2
    listed = alice.get("/api/documents").json()
    assert next(d for d in listed if d["id"] == document_id)["version_number"] == 2
    versions = alice.get("/api/documents/%s/versions" % document_id).json()
    assert [v["number"] for v in versions] == [2, 1]


def test_two_concurrent_loads_leave_the_session_consistent(alice):
    """Two people opening a page together both trigger a load; the second
    must wait for the first rather than replay the log over it."""
    import asyncio

    from app.config import get_settings
    from app.realtime.session import DocumentSession

    document_id = alice.new_document("Race", content="hello")

    async def load_twice():
        session = DocumentSession(document_id, get_settings())
        await asyncio.gather(session.load(), session.load(), session.load())
        return session

    session = asyncio.run(load_twice())
    # Replayed once: one operation in history, not one per load.
    assert session.content == "hello"
    assert session.version == 1
    assert len(session.history) == 1
