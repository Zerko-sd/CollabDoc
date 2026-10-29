"""Authoritative per-document state machine (WBS 5.1 / 5.2).

One ``DocumentSession`` owns the server-side truth for one document: its text,
its version counter, the operation log, and the authorship ledger. Clients send
operations stamped with the version they were written against; the session
transforms each one against everything that landed in the meantime, applies it,
and hands back the transformed operation for the other clients.

Ordering of work inside :meth:`submit` follows the parallel block in the SRS
3.2 sequence diagram: the in-memory state is updated and the transformed
operation is returned for immediate broadcast, while the durable write and the
attribution pass happen off the critical path. That is what keeps fan-out
inside the 200ms budget of SRS 5.1 without giving up the operation log.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..attribution.engine import AttributionEngine, BlockAttribution
from ..attribution.trail import AuthorTrail
from ..config import Settings
from ..db import SessionLocal
from ..models import (
    AuthorSample,
    Document,
    DocumentBlock,
    DocumentOperation,
    DocumentSnapshot,
    utcnow,
)
from .ot import TextOperation
from .versions import VersionCounter


class VersionTooOldError(ValueError):
    """The client's base version predates the operations still in memory."""


@dataclass
class AppliedOperation:
    version: int
    version_number: int
    operation: TextOperation
    user_id: str
    content: str
    latency_ms: float


class DocumentSession:
    """In-memory state for one document, plus its persistence rules."""

    def __init__(self, document_id: str, settings: Settings) -> None:
        self.document_id = document_id
        self.settings = settings
        self.content = ""
        self.version = 0
        self.history: list[TextOperation] = []
        self.trail = AuthorTrail()
        self.lock = asyncio.Lock()

        self.engine = AttributionEngine(
            min_chars=settings.attribution_min_chars,
            min_samples_per_author=settings.attribution_min_samples_per_author,
        )
        self.blocks: list[BlockAttribution] = []
        self.attribution_version = -1

        # What people see as "version N"; ``version`` is the OT revision.
        self.versions = VersionCounter(settings.version_group_idle_seconds)

        self._pending: list[DocumentOperation] = []
        self._ops_since_snapshot = 0
        self._loaded = False
        self._load_lock = asyncio.Lock()

    # -- loading -----------------------------------------------------------

    async def load(self) -> None:
        # Two people opening a page at once both reach here before either
        # load finishes. Running _load_sync twice in parallel threads lets
        # one reset content and version while the other is replaying the
        # log, leaving the room at version 0 with empty text -- so the
        # load is single-flight: the second caller waits for the first.
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            await asyncio.to_thread(self._load_sync)
            self._loaded = True

    def _load_sync(self) -> None:
        with SessionLocal() as db:
            snapshot = db.scalars(
                select(DocumentSnapshot)
                .where(DocumentSnapshot.document_id == self.document_id)
                .order_by(DocumentSnapshot.version.desc())
                .limit(1)
            ).first()

            if snapshot is not None:
                self.content = snapshot.content
                self.version = snapshot.version
                self.trail = AuthorTrail.from_json(json.loads(snapshot.trail_json))
            else:
                self.content, self.version, self.trail = "", 0, AuthorTrail()

            # The state the in-memory history replays forward from.
            self._history_base = self.version
            self._base_content = self.content

            rows = db.scalars(
                select(DocumentOperation)
                .where(
                    DocumentOperation.document_id == self.document_id,
                    DocumentOperation.version > self.version,
                )
                .order_by(DocumentOperation.version)
            ).all()

            for row in rows:
                operation = TextOperation.from_json(row.delta_json)
                self.content = operation.apply(self.content)
                self.trail.apply(operation, row.user_id)
                self.history.append(operation)
                self.version = row.version

            self.versions = VersionCounter.from_database(
                db, self.document_id, self.settings.version_group_idle_seconds
            )

    @property
    def version_number(self) -> int:
        return self.versions.number

    @property
    def history_base(self) -> int:
        """Version the in-memory operation log starts at."""
        return getattr(self, "_history_base", 0)

    @property
    def base_content(self) -> str:
        """Document text at :attr:`history_base`."""
        return getattr(self, "_base_content", "")

    # -- editing -----------------------------------------------------------

    async def submit(
        self, user_id: str, base_version: int, operation: TextOperation
    ) -> AppliedOperation:
        """Transform, apply, and log one client operation."""
        started = time.perf_counter()
        async with self.lock:
            if base_version > self.version:
                raise ValueError(
                    "operation claims version %d but the document is at %d"
                    % (base_version, self.version)
                )
            offset = base_version - self.history_base
            if offset < 0:
                raise VersionTooOldError(
                    "base version %d is older than the loaded history" % base_version
                )

            # Rebase the incoming operation over every operation the server
            # accepted after the client last heard from it.
            transformed = operation
            for concurrent in self.history[offset:]:
                transformed, _ = TextOperation.transform(transformed, concurrent)

            self.content = transformed.apply(self.content)
            self.trail.apply(transformed, user_id)
            self.history.append(transformed)
            self.version += 1
            now = utcnow()
            self.versions.record(user_id, now)

            self._pending.append(
                DocumentOperation(
                    document_id=self.document_id,
                    user_id=user_id,
                    version=self.version,
                    delta_json=transformed.to_json_str(),
                    timestamp=now,
                )
            )
            self._ops_since_snapshot += 1

            return AppliedOperation(
                version=self.version,
                version_number=self.versions.number,
                operation=transformed,
                user_id=user_id,
                content=self.content,
                latency_ms=(time.perf_counter() - started) * 1000,
            )

    def transform_cursor(self, position: int, from_version: int) -> int:
        """Move a caret written against ``from_version`` up to the current one."""
        offset = max(0, from_version - self.history_base)
        for operation in self.history[offset:]:
            position = operation.transform_cursor(position)
        return position

    # -- version history (WBS 4.2) ----------------------------------------

    def content_at(self, version: int) -> str:
        """Rebuild the document as it stood at ``version``.

        Versions still held in memory are replayed forward from the loaded
        base; anything older is replayed from the nearest durable snapshot.
        """
        if version > self.version or version < 0:
            raise ValueError("no such version: %d" % version)
        if version >= self.history_base:
            content = self.base_content
            for operation in self.history[: version - self.history_base]:
                content = operation.apply(content)
            return content
        return _replay_from_database(self.document_id, version)

    async def revert(self, user_id: str, version: int) -> AppliedOperation:
        """Roll the document back by appending the operation that undoes it.

        History stays append-only, so a rollback is itself an auditable edit
        rather than a hole in the operation log.
        """
        target = await asyncio.to_thread(self.content_at, version)
        operation = TextOperation.from_diff(self.content, target)
        return await self.submit(user_id, self.version, operation)

    # -- persistence -------------------------------------------------------

    def _drain(self) -> tuple[list[DocumentOperation], bool, str, int, int, str]:
        """Take everything waiting to be written. Contains no ``await``."""
        pending, self._pending = self._pending, []
        take_snapshot = self._ops_since_snapshot >= self.settings.snapshot_every_n_ops
        if take_snapshot:
            self._ops_since_snapshot = 0
        return (
            pending,
            take_snapshot,
            self.content,
            self.version,
            self.versions.number,
            json.dumps(self.trail.to_json()),
        )

    async def flush(self) -> int:
        """Write queued operations. Returns how many rows were written."""
        async with self.lock:
            batch = self._drain()
        if not batch[0] and not batch[1]:
            return 0
        return await asyncio.to_thread(self._flush_sync, *batch)

    def flush_blocking(self) -> int:
        """Write queued operations without suspending.

        The teardown path runs on a task that the server has already cancelled
        -- a client that closed its tab, typically -- where every ``await``
        raises ``CancelledError`` before it completes. Doing the last write
        inline costs a few milliseconds on a path nobody is waiting on, and is
        the difference between keeping the final second of typing and losing
        it.
        """
        batch = self._drain()
        if not batch[0] and not batch[1]:
            return 0
        return self._flush_sync(*batch)

    def _flush_sync(
        self,
        pending: list[DocumentOperation],
        take_snapshot: bool,
        content: str,
        version: int,
        version_number: int,
        trail_json: str,
    ) -> int:
        with SessionLocal() as db:
            for row in pending:
                db.add(row)
            document = db.get(Document, self.document_id)
            if document is not None:
                document.current_version = version
                document.version_number = version_number
                document.updated_at = utcnow()
            if take_snapshot:
                self._write_snapshot(db, version, content, trail_json, label=None)
            db.commit()
        return len(pending)

    async def save_snapshot(self, label: str | None = None) -> int:
        """Manual save (SRS 3.3): flush, then pin a labelled snapshot."""
        await self.flush()
        async with self.lock:
            content, version = self.content, self.version
            trail = json.dumps(self.trail.to_json())
            if label:
                self.versions.close()

        def _write() -> int:
            with SessionLocal() as db:
                self._write_snapshot(db, version, content, trail, label)
                db.commit()
            return version

        return await asyncio.to_thread(_write)

    def _write_snapshot(
        self,
        db: Session,
        version: int,
        content: str,
        trail_json: str,
        label: str | None,
    ) -> None:
        existing = db.scalars(
            select(DocumentSnapshot).where(
                DocumentSnapshot.document_id == self.document_id,
                DocumentSnapshot.version == version,
            )
        ).first()
        if existing is not None:
            existing.content = content
            existing.trail_json = trail_json
            if label:
                existing.label = label
            return
        db.add(
            DocumentSnapshot(
                document_id=self.document_id,
                version=version,
                content=content,
                trail_json=trail_json,
                label=label,
            )
        )

    # -- attribution (WBS 6.3) --------------------------------------------

    async def refresh_attribution(self, force: bool = False) -> list[BlockAttribution]:
        """Retrain on what is known and re-attribute every block.

        Runs in a worker thread: training is the one part of the pipeline that
        is CPU-bound, and it must not stall the event loop that is fanning out
        edits to the other clients.
        """
        if not force and self.attribution_version == self.version:
            return self.blocks
        async with self.lock:
            content, version = self.content, self.version
            trail = AuthorTrail.from_json(self.trail.to_json())
        blocks = await asyncio.to_thread(self._attribute_sync, content, trail)
        self.blocks = blocks
        self.attribution_version = version
        return blocks

    def _attribute_sync(
        self, content: str, trail: AuthorTrail
    ) -> list[BlockAttribution]:
        corpus: dict[str, list[str]] = {
            user: list(chunks) for user, chunks in trail.text_by_author(content).items()
        }
        with SessionLocal() as db:
            # Prior writing by the same collaborators, from documents they have
            # already worked on, sharpens a model that this document alone
            # could not train.
            samples = db.scalars(
                select(AuthorSample).where(
                    AuthorSample.user_id.in_(corpus.keys() or [""])
                )
            ).all()
            for sample in samples:
                corpus.setdefault(sample.user_id, []).append(sample.content)

            self.engine.train(corpus)
            blocks = self.engine.attribute_document(content, trail)
            self._persist_blocks(db, blocks)
            db.commit()
        return blocks

    def _persist_blocks(self, db: Session, blocks: list[BlockAttribution]) -> None:
        existing = db.scalars(
            select(DocumentBlock)
            .where(DocumentBlock.document_id == self.document_id)
            .order_by(DocumentBlock.sort_order)
        ).all()
        for index, block in enumerate(blocks):
            if index < len(existing):
                row = existing[index]
            else:
                row = DocumentBlock(document_id=self.document_id)
                db.add(row)
            row.block_type = block.block_type
            row.content = block.content
            row.sort_order = block.sort_order
            row.attributed_user_id = block.attributed_user_id
            row.confidence_score = block.confidence_score
            row.updated_at = utcnow()
        for stale in existing[len(blocks) :]:
            db.delete(stale)

    def attribution_state(self) -> dict:
        # Deliberately not called "version": this is spliced into the init
        # frame alongside the document's own version.
        return {
            "attribution_version": self.attribution_version,
            "model": self.engine.report.to_dict(),
            "blocks": [block.to_dict() for block in self.blocks],
        }



def _replay_from_database(document_id: str, version: int) -> str:
    """Reconstruct a version by replaying the log from the nearest snapshot."""
    with SessionLocal() as db:
        snapshot = db.scalars(
            select(DocumentSnapshot)
            .where(
                DocumentSnapshot.document_id == document_id,
                DocumentSnapshot.version <= version,
            )
            .order_by(DocumentSnapshot.version.desc())
            .limit(1)
        ).first()
        content = snapshot.content if snapshot else ""
        start = snapshot.version if snapshot else 0
        rows = db.scalars(
            select(DocumentOperation)
            .where(
                DocumentOperation.document_id == document_id,
                DocumentOperation.version > start,
                DocumentOperation.version <= version,
            )
            .order_by(DocumentOperation.version)
        ).all()
        for row in rows:
            content = TextOperation.from_json(row.delta_json).apply(content)
        return content
