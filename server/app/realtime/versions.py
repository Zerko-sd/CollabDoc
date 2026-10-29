"""User-facing version numbering.

The operation log advances once per keystroke because OT needs that
granularity; people do not think in keystrokes. A *version* is a burst of
editing: consecutive operations by one author with no pause longer than
``version_group_idle_seconds``. A labelled save always closes the burst it
ends, so a named save point is never merged with what follows it.

The rule is pairwise between neighbouring operations, so walking the log
forwards (to count versions) and backwards (to list the newest ones) cut it
in the same places.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DocumentOperation, DocumentSnapshot


def as_utc(moment: datetime) -> datetime:
    """SQLite hands back naive datetimes; everything here is UTC."""
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def same_version(
    earlier_user: str,
    earlier_at: datetime,
    earlier_labelled: bool,
    later_user: str,
    later_at: datetime,
    idle: timedelta,
) -> bool:
    """Whether the later of two neighbouring operations extends the earlier's version."""
    return (
        not earlier_labelled
        and earlier_user == later_user
        and as_utc(later_at) - as_utc(earlier_at) <= idle
    )


def labelled_versions(db: Session, document_id: str) -> dict[int, str]:
    """OT revision -> label for every named save point."""
    return dict(
        db.execute(
            select(DocumentSnapshot.version, DocumentSnapshot.label).where(
                DocumentSnapshot.document_id == document_id,
                DocumentSnapshot.label.is_not(None),
            )
        ).all()
    )


class VersionCounter:
    """Tracks the user-facing version number as operations arrive."""

    def __init__(self, idle_seconds: float) -> None:
        self.idle = timedelta(seconds=idle_seconds)
        self.number = 0
        self._user: str | None = None
        self._at: datetime | None = None
        self._closed = False

    def record(self, user_id: str, at: datetime) -> int:
        if self._user is None or not same_version(
            self._user, self._at, self._closed, user_id, at, self.idle
        ):
            self.number += 1
        self._user, self._at, self._closed = user_id, at, False
        return self.number

    def close(self) -> None:
        """A labelled save: whatever comes next is a new version."""
        self._closed = True

    @classmethod
    def from_database(
        cls, db: Session, document_id: str, idle_seconds: float
    ) -> "VersionCounter":
        counter = cls(idle_seconds)
        labels = labelled_versions(db, document_id)
        rows = db.execute(
            select(
                DocumentOperation.version,
                DocumentOperation.user_id,
                DocumentOperation.timestamp,
            )
            .where(DocumentOperation.document_id == document_id)
            .order_by(DocumentOperation.version)
        )
        for version, user_id, timestamp in rows:
            counter.record(user_id, timestamp)
            if version in labels:
                counter.close()
        return counter
