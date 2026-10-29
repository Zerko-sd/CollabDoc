"""Recording actions on a page for its audit log."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import AuditEvent, User


def record(db: Session, document_id: str, actor: User | None, action: str, detail: str = "") -> None:
    """Add an audit entry to the session; the caller's commit writes it.

    Riding on the caller's transaction means an action and its log entry
    land together or not at all.
    """
    db.add(
        AuditEvent(
            document_id=document_id,
            actor_id=actor.id if actor else None,
            action=action,
            detail=detail[:280],
        )
    )
