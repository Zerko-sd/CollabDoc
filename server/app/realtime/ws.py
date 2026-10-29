"""WebSocket sync endpoint (WBS 5.2, SRS 4.4).

Protocol, client to server::

    {"type": "op",     "version": <int>, "operation": [...]}
    {"type": "cursor", "version": <int>, "position": <int>, "selection_end": <int>}
    {"type": "save",   "label": <str|null>}
    {"type": "ping"}

and server to client::

    {"type": "init",       "document_id", "version", "version_number", "content", "you", "users", ...}
    {"type": "ack",        "version", "version_number", "server_latency_ms"}
    {"type": "op",         "version", "version_number", "operation", "user_id"}
    {"type": "saved",      "version", "version_number"}

``version`` is the OT revision and moves on every operation; ``version_number``
is the one people see, and moves once per editing burst (see versions.py).
    {"type": "presence",   "users": [...]}
    {"type": "cursor",     "user_id", "connection_id", "position", "selection_end"}
    {"type": "attribution","attribution_version", "blocks", "model"}
    {"type": "error",      "code", "message"}

The sender of an operation gets an ``ack`` rather than an echo of its own
edit, which is what lets the client keep exactly one operation in flight and
buffer the rest (the standard OT client state machine).
"""
from __future__ import annotations

import contextlib
import json

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..deps import resolve_access
from ..models import Role, User
from ..notifications import user_channel
from ..security import decode_access_token
from .hub import Participant, RoomRegistry
from .ot import OTError, TextOperation
from .session import VersionTooOldError

router = APIRouter()

# Close codes beyond the RFC set, so the client can tell the two apart.
CLOSE_UNAUTHORISED = 4401
CLOSE_FORBIDDEN = 4403


@router.websocket("/ws/documents/{document_id}")
async def document_socket(
    websocket: WebSocket,
    document_id: str,
    token: str = Query(default=""),
) -> None:
    user, role = _authorise(document_id, token)
    if user is None:
        await websocket.close(code=CLOSE_UNAUTHORISED, reason="Invalid token")
        return
    if role is None:
        await websocket.close(code=CLOSE_FORBIDDEN, reason="No access to this document")
        return

    registry: RoomRegistry = websocket.app.state.rooms
    await websocket.accept()
    room = await registry.get(document_id)
    participant = Participant(
        websocket=websocket,
        user_id=user.id,
        name=user.name,
        presence_color=user.presence_color,
        role=role,
    )

    await websocket.send_json(
        {
            "type": "init",
            "document_id": document_id,
            "version": room.session.version,
            "version_number": room.session.version_number,
            "content": room.session.content,
            "you": {
                "user_id": user.id,
                "name": user.name,
                "presence_color": user.presence_color,
                "role": role.value,
                "connection_id": participant.connection_id,
                "can_edit": role.can(Role.EDITOR),
            },
            **room.session.attribution_state(),
        }
    )
    await room.join(participant)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await _error(websocket, "bad_json", "Message was not valid JSON")
                continue
            await _handle(room, participant, message)
    except WebSocketDisconnect:
        pass
    finally:
        # The server cancels this task the moment the socket drops, so the
        # durable part of the teardown is synchronous and sits in a `finally`
        # that a cancelled presence broadcast cannot skip.
        room.remove(participant)
        try:
            if not room.is_empty():
                await room.publish_presence()
        finally:
            registry.release_now(document_id)


@router.websocket("/ws/me")
async def user_socket(websocket: WebSocket, token: str = Query(default="")) -> None:
    """One socket per signed-in tab for things addressed to the user rather
    than to a document -- notifications, today.

    Server to client: ``{"type": "notification", "notification": {...}}``.
    Client to server: ``{"type": "ping"}`` only.
    """
    user_id = decode_access_token(token)
    with SessionLocal() as db:
        known = user_id is not None and db.get(User, user_id) is not None
    if not known:
        await websocket.close(code=CLOSE_UNAUTHORISED, reason="Invalid token")
        return

    registry: RoomRegistry = websocket.app.state.rooms
    channel = user_channel(user_id)
    await websocket.accept()

    async def deliver(message: dict) -> None:
        with contextlib.suppress(Exception):
            await websocket.send_json(message)

    await registry.broadcaster.subscribe(channel, deliver)
    try:
        while True:
            raw = await websocket.receive_text()
            with contextlib.suppress(json.JSONDecodeError):
                if json.loads(raw).get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        # Synchronous on purpose, like the document rooms' teardown.
        registry.broadcaster.detach(channel, deliver)


async def _handle(room, participant: Participant, message: dict) -> None:
    kind = message.get("type")

    if kind == "ping":
        await participant.websocket.send_json({"type": "pong"})
        return

    if kind == "cursor":
        position = int(message.get("position", 0))
        selection_end = int(message.get("selection_end", position))
        base = int(message.get("version", room.session.version))
        # A caret sent against an older version has to be carried forward over
        # everything that landed since, or it points at the wrong character.
        participant.cursor = room.session.transform_cursor(position, base)
        participant.selection_end = room.session.transform_cursor(selection_end, base)
        participant.cursor_version = room.session.version
        await room.publish(
            {
                "type": "cursor",
                "user_id": participant.user_id,
                "connection_id": participant.connection_id,
                "name": participant.name,
                "presence_color": participant.presence_color,
                "position": participant.cursor,
                "selection_end": participant.selection_end,
                "_exclude": participant.connection_id,
            }
        )
        return

    if kind == "save":
        if not participant.role.can(Role.EDITOR):
            await _error(participant.websocket, "forbidden", "Viewers cannot save")
            return
        version = await room.session.save_snapshot(message.get("label"))
        await participant.websocket.send_json(
            {
                "type": "saved",
                "version": version,
                "version_number": room.session.version_number,
            }
        )
        return

    if kind == "op":
        if not participant.role.can(Role.EDITOR):
            await _error(
                participant.websocket, "forbidden", "You have view-only access"
            )
            return
        try:
            operation = TextOperation.from_json(message.get("operation", []))
        except (OTError, TypeError) as exc:
            await _error(participant.websocket, "bad_operation", str(exc))
            return

        try:
            applied = await room.session.submit(
                participant.user_id, int(message.get("version", 0)), operation
            )
        except VersionTooOldError as exc:
            # The client is too far behind to be rebased; it must resynchronise.
            await _error(participant.websocket, "resync_required", str(exc))
            return
        except (OTError, ValueError) as exc:
            await _error(participant.websocket, "rejected", str(exc))
            return

        # One broadcast: the author's socket receives it as the ack, everyone
        # else as the operation, all in version order (DocumentRoom._deliver).
        # Acking directly would let an ack overtake an earlier broadcast.
        await room.publish(
            {
                "type": "op",
                "version": applied.version,
                "version_number": applied.version_number,
                "operation": applied.operation.to_json(),
                "user_id": participant.user_id,
                "server_latency_ms": round(applied.latency_ms, 3),
                "_ack": participant.connection_id,
            }
        )
        room.signal_edit()
        return

    await _error(participant.websocket, "unknown_type", "Unsupported message type")


async def _error(websocket: WebSocket, code: str, message: str) -> None:
    try:
        await websocket.send_json({"type": "error", "code": code, "message": message})
    except Exception:  # pragma: no cover - the socket is already gone
        pass


def _authorise(document_id: str, token: str) -> tuple[User | None, Role | None]:
    """Resolve the token and the caller's role on the document.

    Runs against its own database session because a WebSocket route cannot use
    the request-scoped dependency that the REST routes share.
    """
    user_id = decode_access_token(token)
    if user_id is None:
        return None, None
    with SessionLocal() as db:  # type: Session
        user = db.get(User, user_id)
        if user is None:
            return None, None
        try:
            access = resolve_access(db, document_id, user)
        except Exception:
            return user, None
        db.expunge(user)
        return user, access.role


__all__ = ["router", "CLOSE_UNAUTHORISED", "CLOSE_FORBIDDEN"]
