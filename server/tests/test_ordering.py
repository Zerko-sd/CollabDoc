"""Operations reach every socket in version order, acknowledgements included.

Found by scripts/load_test.py: with 100 clients typing at once, an ack sent
straight to its author could overtake the broadcast of an earlier operation,
and the author's client then rebased that operation onto the wrong text.
"""
from __future__ import annotations

import asyncio
import json

from app.models import Role
from app.realtime.broadcaster import InMemoryBroadcaster
from app.realtime.hub import DocumentRoom, Participant
from app.realtime.ot import TextOperation

from test_realtime import receive_until, socket


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


class FakeSession:
    version = 4


def _op(version: int, author: str) -> dict:
    return {
        "type": "op",
        "version": version,
        "version_number": 1,
        "operation": [1],
        "user_id": author,
        "server_latency_ms": 0.1,
        "_ack": author,
    }


def _room() -> tuple[DocumentRoom, FakeSocket, FakeSocket]:
    room = DocumentRoom("doc", FakeSession(), InMemoryBroadcaster())
    room._next_version = 5
    a, b = FakeSocket(), FakeSocket()
    for name, ws in (("a", a), ("b", b)):
        room.participants[name] = Participant(ws, name, name, "#000000", Role.EDITOR, connection_id=name)
    return room, a, b


def test_an_ack_never_overtakes_an_earlier_operation():
    async def scenario():
        room, a, b = _room()
        # Version 6 (b's) is published before version 5 (a's) gets there.
        await room._deliver(_op(6, "b"))
        assert b.sent == [] and a.sent == []
        await room._deliver(_op(5, "a"))
        return a, b

    a, b = asyncio.run(scenario())
    assert [(m["type"], m["version"]) for m in a.sent] == [("ack", 5), ("op", 6)]
    assert [(m["type"], m["version"]) for m in b.sent] == [("op", 5), ("ack", 6)]
    # The routing keys stay on the server.
    assert all("_ack" not in m and "_exclude" not in m for m in a.sent + b.sent)


def test_a_version_that_never_arrives_does_not_stall_the_room():
    async def scenario():
        room, a, b = _room()
        await room._deliver(_op(7, "b"))  # 5 and 6 are lost
        await asyncio.sleep(1.2)
        return a, b

    a, b = asyncio.run(scenario())
    assert [(m["type"], m["version"]) for m in a.sent] == [("op", 7)]
    assert [(m["type"], m["version"]) for m in b.sent] == [("ack", 7)]


def test_a_rest_save_reaches_people_with_the_page_open(alice):
    document_id = alice.new_document("Live", "Hello")
    with socket(alice, document_id) as ws:
        receive_until(ws, "init")
        saved = alice.post("/api/documents/%s/save" % document_id, json={"content": "Hello world"})
        assert saved.status_code == 200
        message = receive_until(ws, "op")
        assert TextOperation.from_json(message["operation"]).apply("Hello") == "Hello world"
