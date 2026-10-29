"""Document rooms: connections, presence, and the background workers (WBS 5.2/5.3).

A room is everything that is live about one open document -- the authoritative
:class:`DocumentSession`, the sockets currently attached, who is where in the
text, and two background workers:

* a **saver** that drains the operation queue on a fixed cadence (the
  auto-save of SRS 3.3, kept off the edit path so a disk write never delays a
  broadcast);
* an **attributor** that retrains the stylometric model and re-attributes the
  blocks once the typing settles, then pushes the result to everyone.

Outbound messages leave through the broadcaster and arrive back through the
room's subscription, so a second backend running the same code would deliver
them to its own sockets without any extra plumbing (SRS 2.4).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from ..config import Settings, get_settings
from ..models import Role
from .broadcaster import Broadcaster, build_broadcaster
from .session import DocumentSession

logger = logging.getLogger(__name__)

# How often the saver drains queued operations.
SAVE_INTERVAL_SECONDS = 1.0
# Quiet period after the last edit before the attribution pass runs. Long
# enough that a burst of typing trains once rather than once per keystroke.
ATTRIBUTION_DEBOUNCE_SECONDS = 1.5
# Backoff before retrying an attribution pass that raised.
ATTRIBUTION_RETRY_SECONDS = 2.0


@dataclass
class Participant:
    """One open editor tab."""

    websocket: WebSocket
    user_id: str
    name: str
    presence_color: str
    role: Role
    connection_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cursor: int = 0
    selection_end: int = 0
    cursor_version: int = 0
    joined_at: float = field(default_factory=time.monotonic)

    def presence(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "user_id": self.user_id,
            "name": self.name,
            "presence_color": self.presence_color,
            "role": self.role.value,
            "cursor": self.cursor,
            "selection_end": self.selection_end,
        }


class DocumentRoom:
    """Live state for one document."""

    def __init__(
        self, document_id: str, session: DocumentSession, broadcaster: Broadcaster
    ) -> None:
        self.document_id = document_id
        self.session = session
        self.broadcaster = broadcaster
        self.channel = "document:%s" % document_id
        self.participants: dict[str, Participant] = {}
        self._subscribed = False
        self._saver: asyncio.Task | None = None
        self._attributor: asyncio.Task | None = None
        self._edit_signal = asyncio.Event()
        self._closing = False
        # Operations reach sockets strictly in version order (see _deliver).
        self._next_version: int | None = None
        self._held: dict[int, dict[str, Any]] = {}
        self._order = asyncio.Lock()
        self._gap_timer: asyncio.TimerHandle | None = None

    # -- membership --------------------------------------------------------

    def _spawn(self, coro, name: str) -> asyncio.Task:
        """Start a background worker that reports its own death."""
        task = asyncio.create_task(coro)

        def done(finished: asyncio.Task) -> None:
            if finished.cancelled():
                return
            error = finished.exception()
            if error is not None:
                logger.error(
                    "the %s worker for document %s stopped: %r",
                    name,
                    self.document_id,
                    error,
                    exc_info=error,
                )

        task.add_done_callback(done)
        return task

    async def join(self, participant: Participant) -> None:
        if not self._subscribed:
            self._next_version = self.session.version + 1
            await self.broadcaster.subscribe(self.channel, self._deliver)
            self._subscribed = True
        if self._saver is None:
            self._saver = self._spawn(self._save_loop(), "saver")
        if self._attributor is None:
            self._attributor = self._spawn(self._attribution_loop(), "attributor")
        self.participants[participant.connection_id] = participant
        await self.publish_presence()

    def remove(self, participant: Participant) -> None:
        """Detach a socket. Synchronous, for the same reason as close_now."""
        self.participants.pop(participant.connection_id, None)

    def is_empty(self) -> bool:
        return not self.participants

    def close_now(self) -> int:
        """Tear the room down synchronously. Returns operations written.

        Contains no ``await`` on purpose: this runs from the disconnect path,
        where the task has usually been cancelled already and any suspension
        point would raise before the queued operations reached the database.
        """
        self._closing = True
        if self._gap_timer is not None:
            self._gap_timer.cancel()
        written = self.session.flush_blocking()
        for task in (self._saver, self._attributor):
            if task is not None:
                task.cancel()
        if self._subscribed:
            self.broadcaster.detach(self.channel, self._deliver)
            self._subscribed = False
        return written

    async def close(self) -> None:
        """Tear the room down and wait for its workers to stop."""
        workers = [t for t in (self._saver, self._attributor) if t is not None]
        self.close_now()
        self._saver = self._attributor = None
        for task in workers:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # -- messaging ---------------------------------------------------------

    async def publish(self, message: dict[str, Any]) -> None:
        await self.broadcaster.publish(self.channel, message)

    async def _deliver(self, message: dict[str, Any]) -> None:
        """Deliver a broadcast, holding back operations that arrive early.

        The OT protocol needs every client to see the operations in the order
        the server numbered them, with its own acknowledgement in its place
        in that sequence: an ack for version 6 that overtakes the broadcast
        of version 5 makes the client rebase version 5 onto the wrong text.
        Two concurrent submissions can reach this point in either order, so
        an operation numbered ahead of the next expected one waits here
        until the gap is filled (or, if it never is, for a second).
        """
        version = message.get("version") if message.get("type") == "op" else None
        async with self._order:
            if version is None or self._next_version is None:
                await self._send(message)
                return
            if version > self._next_version:
                self._held[version] = message
                self._arm_gap_timer()
                return
            await self._send(message)
            self._next_version = max(self._next_version, version + 1)
            await self._release_held()

    async def _release_held(self) -> None:
        while self._next_version in self._held:
            await self._send(self._held.pop(self._next_version))
            self._next_version += 1
        if not self._held and self._gap_timer is not None:
            self._gap_timer.cancel()
            self._gap_timer = None

    def _arm_gap_timer(self) -> None:
        if self._gap_timer is not None:
            return
        loop = asyncio.get_running_loop()
        self._gap_timer = loop.call_later(1.0, lambda: loop.create_task(self._skip_gap()))

    async def _skip_gap(self) -> None:
        """A version never arrived: deliver what is held rather than stall."""
        async with self._order:
            self._gap_timer = None
            if not self._held or self._closing:
                return
            logger.warning(
                "document %s: version %s never reached the room; skipping ahead",
                self.document_id,
                self._next_version,
            )
            self._next_version = min(self._held)
            await self._release_held()

    async def _send(self, message: dict[str, Any]) -> None:
        """Send one broadcast message to every local socket it is meant for.

        An operation names its author's connection in ``_ack``: that socket
        gets the acknowledgement, in sequence, and everyone else the operation.
        """
        exclude = message.get("_exclude")
        ack_to = message.get("_ack")
        payload = {k: v for k, v in message.items() if k not in ("_exclude", "_ack", "server_latency_ms")}
        ack = None
        if ack_to is not None:
            ack = {
                "type": "ack",
                "version": message["version"],
                "version_number": message.get("version_number"),
                "server_latency_ms": message.get("server_latency_ms"),
            }
        # Encoded once, not once per socket: with a hundred people in a room
        # that is a hundred fewer JSON encodings per keystroke.
        text = json.dumps(payload, separators=(",", ":"))
        ack_text = json.dumps(ack, separators=(",", ":")) if ack is not None else None
        dead: list[Participant] = []
        for participant in list(self.participants.values()):
            if exclude is not None and participant.connection_id == exclude:
                continue
            try:
                if ack_text is not None and participant.connection_id == ack_to:
                    await participant.websocket.send_text(ack_text)
                else:
                    await participant.websocket.send_text(text)
            except Exception:
                # The socket is gone; drop it rather than failing the fan-out
                # for everybody else.
                dead.append(participant)
        for participant in dead:
            self.participants.pop(participant.connection_id, None)

    async def publish_presence(self) -> None:
        await self.publish(
            {
                "type": "presence",
                "users": [p.presence() for p in self.participants.values()],
            }
        )

    def signal_edit(self) -> None:
        self._edit_signal.set()

    # -- background workers ------------------------------------------------

    async def _save_loop(self) -> None:
        while not self._closing:
            await asyncio.sleep(SAVE_INTERVAL_SECONDS)
            try:
                await self.session.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed write must not kill the saver; the operations stay
                # queued and the next tick retries them. It must not pass
                # silently either.
                logger.exception("saving document %s failed", self.document_id)

    async def _attribution_loop(self) -> None:
        while not self._closing:
            await self._edit_signal.wait()
            # Coalesce a burst of edits into one training pass.
            while True:
                self._edit_signal.clear()
                try:
                    await asyncio.wait_for(
                        self._edit_signal.wait(), ATTRIBUTION_DEBOUNCE_SECONDS
                    )
                except asyncio.TimeoutError:
                    break
            try:
                await self.session.refresh_attribution()
                await self.publish(
                    {"type": "attribution", **self.session.attribution_state()}
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # The broadcast is inside the guard alongside the pass: an
                # error there would otherwise escape the loop and kill the
                # worker, freezing attribution for the life of the room.
                # Re-arm rather than falling back to waiting for an edit that
                # may never come, since typing usually stops before anyone
                # notices the output has gone stale.
                logger.exception(
                    "attributing document %s failed; retrying", self.document_id
                )
                await asyncio.sleep(ATTRIBUTION_RETRY_SECONDS)
                self._edit_signal.set()


class RoomRegistry:
    """Process-wide map of open documents."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.broadcaster = build_broadcaster(self.settings.broadcaster, self.settings.kafka_topic)
        self._rooms: dict[str, DocumentRoom] = {}
        self._lock = asyncio.Lock()

    @property
    def open_documents(self) -> int:
        """How many documents currently have someone editing them."""
        return len(self._rooms)

    async def start(self) -> None:
        await self.broadcaster.start()

    async def stop(self) -> None:
        async with self._lock:
            rooms = list(self._rooms.values())
            self._rooms.clear()
        for room in rooms:
            await room.close()
        await self.broadcaster.stop()

    async def get(self, document_id: str) -> DocumentRoom:
        async with self._lock:
            room = self._rooms.get(document_id)
            if room is None:
                session = DocumentSession(document_id, self.settings)
                room = DocumentRoom(document_id, session, self.broadcaster)
                self._rooms[document_id] = room
        await room.session.load()
        return room

    def release_now(self, document_id: str) -> int:
        """Close an empty room synchronously; a no-op while anyone remains.

        No lock is taken because the whole method runs without suspending, so
        no other task can observe the registry mid-change.
        """
        room = self._rooms.get(document_id)
        if room is None or not room.is_empty():
            return 0
        del self._rooms[document_id]
        return room.close_now()

    async def release(self, document_id: str) -> None:
        """Tear a room down once the last editor leaves, awaiting its workers."""
        async with self._lock:
            room = self._rooms.get(document_id)
            if room is None or not room.is_empty():
                return
            del self._rooms[document_id]
        await room.close()

    async def force_close(self, document_id: str) -> None:
        """Shut a room down regardless of who is still attached.

        Used when the document is about to be deleted: the saver has to stop
        before the row goes away, or it would write operations against an id
        that no longer exists.
        """
        async with self._lock:
            room = self._rooms.pop(document_id, None)
        if room is not None:
            await room.close()

    async def peek(self, document_id: str) -> DocumentRoom | None:
        """The room for a document if it is open, without opening one."""
        async with self._lock:
            return self._rooms.get(document_id)
