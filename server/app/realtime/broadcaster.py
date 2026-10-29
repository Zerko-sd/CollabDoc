"""Fan-out transport for document events (SRS 2.4 architecture).

The deployment diagram puts several FastAPI backends behind a load balancer,
with Kafka carrying document events between them: a client connected to
backend A must see edits that arrived at backend B. Every event a room emits
therefore goes out through a Broadcaster and comes back in through a
subscription, even when the only subscriber is the same process that published
it.

Keeping that round trip in the single-node path means the multi-node path is
not a different code path -- swapping ``COLLABDOC_BROADCASTER`` for a Kafka
implementation of this interface is the whole change.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

Handler = Callable[[dict[str, Any]], Awaitable[None]]

logger = logging.getLogger(__name__)


class Broadcaster:
    """Publish/subscribe over named channels (one channel per document)."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def subscribe(self, channel: str, handler: Handler) -> None:
        raise NotImplementedError

    async def unsubscribe(self, channel: str, handler: Handler) -> None:
        raise NotImplementedError

    def detach(self, channel: str, handler: Handler) -> None:
        """Drop a subscription synchronously.

        Teardown runs on a task that may already be cancelled, where any
        ``await`` would be interrupted, so unsubscribing has to be possible
        without one.
        """
        raise NotImplementedError

    async def publish(self, channel: str, message: dict[str, Any]) -> None:
        raise NotImplementedError


class InMemoryBroadcaster(Broadcaster):
    """Single-process implementation, used for local runs and tests.

    Delivery is ordered per channel: a lock around dispatch keeps two
    concurrently published operations from reaching subscribers out of the
    sequence the server assigned them.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, channel: str) -> asyncio.Lock:
        lock = self._locks.get(channel)
        if lock is None:
            lock = self._locks[channel] = asyncio.Lock()
        return lock

    async def subscribe(self, channel: str, handler: Handler) -> None:
        self._handlers.setdefault(channel, []).append(handler)

    async def unsubscribe(self, channel: str, handler: Handler) -> None:
        self.detach(channel, handler)

    def detach(self, channel: str, handler: Handler) -> None:
        handlers = self._handlers.get(channel)
        if not handlers:
            return
        if handler in handlers:
            handlers.remove(handler)
        if not handlers:
            self._handlers.pop(channel, None)
            self._locks.pop(channel, None)

    async def publish(self, channel: str, message: dict[str, Any]) -> None:
        handlers = list(self._handlers.get(channel, ()))
        if not handlers:
            return
        async with self._lock(channel):
            for handler in handlers:
                await handler(message)


class KafkaBroadcaster(InMemoryBroadcaster):
    """Kafka-backed fan-out for the multi-node deployment of SRS 2.4.

    One topic for the whole service, keyed by channel so a document's events
    keep their order on a single partition. Every backend joins with its own
    consumer group, so each one receives every event and delivers it to the
    sockets it holds -- including events it published itself, which is the
    same publish-and-come-back round trip the in-memory broadcaster makes.

    Handler bookkeeping and ordered local delivery are inherited; only the
    transport differs. ``producer_factory`` and ``consumer_factory`` exist
    so tests can run the whole path without a broker.
    """

    def __init__(
        self,
        bootstrap_servers: str,
        topic: str = "collabdoc.events",
        *,
        producer_factory=None,
        consumer_factory=None,
    ) -> None:
        super().__init__()
        self.bootstrap_servers = bootstrap_servers
        self.topic = topic
        # Unique per process: a shared group would split the events between
        # backends instead of giving every backend all of them.
        self.group_id = "collabdoc-%s" % uuid.uuid4().hex[:12]
        self._producer_factory = producer_factory or self._default_producer
        self._consumer_factory = consumer_factory or self._default_consumer
        self._producer = None
        self._consumer = None
        self._task: asyncio.Task | None = None

    def _default_producer(self):
        from aiokafka import AIOKafkaProducer

        return AIOKafkaProducer(
            bootstrap_servers=self.bootstrap_servers,
            key_serializer=lambda key: key.encode(),
            value_serializer=lambda value: json.dumps(value, separators=(",", ":")).encode(),
            acks=1,
            linger_ms=0,  # an edit is latency-bound, not throughput-bound
        )

    def _default_consumer(self):
        from aiokafka import AIOKafkaConsumer

        return AIOKafkaConsumer(
            self.topic,
            bootstrap_servers=self.bootstrap_servers,
            group_id=self.group_id,
            # A backend only cares about events from the moment it joins:
            # anything older is already in the operation log.
            auto_offset_reset="latest",
            key_deserializer=lambda key: key.decode() if key else "",
            value_deserializer=lambda value: json.loads(value.decode()),
        )

    async def start(self) -> None:
        self._producer = self._producer_factory()
        self._consumer = self._consumer_factory()
        await self._producer.start()
        await self._consumer.start()
        self._task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        for client in (self._consumer, self._producer):
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.stop()
        self._consumer = self._producer = None

    async def publish(self, channel: str, message: dict[str, Any]) -> None:
        if self._producer is None:
            raise RuntimeError("KafkaBroadcaster.publish before start()")
        await self._producer.send_and_wait(self.topic, value=message, key=channel)

    async def _consume(self) -> None:
        async for record in self._consumer:
            try:
                # Local delivery, in arrival order, to this node's sockets.
                await InMemoryBroadcaster.publish(self, record.key, record.value)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("delivering a Kafka event on %s failed", record.key)


def build_broadcaster(kind: str, topic: str = "collabdoc.events") -> Broadcaster:
    if kind == "memory":
        return InMemoryBroadcaster()
    if kind.startswith("kafka"):
        _, _, servers = kind.partition(":")
        return KafkaBroadcaster(servers or "localhost:9092", topic)
    raise ValueError("unknown broadcaster backend: %r" % (kind,))
