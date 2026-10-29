"""The inbox: mentions, replies, and pages shared with you."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import DocumentAccess, get_current_user, require_editor
from ..models import Document, Notification, NotificationKind, User
from ..notifications import notification_out, notify
from ..realtime.hub import RoomRegistry
from ..schemas import MentionCreate, NotificationOut, NotificationsRead
from .documents import get_registry

router = APIRouter(prefix="/api", tags=["notifications"])


@router.get("/notifications", response_model=list[NotificationOut])
def list_notifications(
    unread: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[NotificationOut]:
    """Newest first. Notifications about pages now in the trash are left out."""
    query = (
        select(Notification)
        .join(Document, Document.id == Notification.document_id)
        .where(Notification.user_id == user.id, Document.deleted_at.is_(None))
        .order_by(Notification.created_at.desc())
        .limit(limit)
    )
    if unread:
        query = query.where(Notification.read_at.is_(None))
    return [notification_out(row) for row in db.scalars(query)]


@router.post("/notifications/read", status_code=status.HTTP_204_NO_CONTENT)
def mark_read(
    payload: NotificationsRead,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    query = (
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(timezone.utc))
    )
    if payload.ids is not None:
        query = query.where(Notification.id.in_(payload.ids))
    db.execute(query)
    db.commit()


@router.post(
    "/documents/{document_id}/mentions",
    response_model=NotificationOut | None,
    status_code=status.HTTP_201_CREATED,
)
async def mention(
    payload: MentionCreate,
    access: DocumentAccess = Depends(require_editor),
    registry: RoomRegistry = Depends(get_registry),
    db: Session = Depends(get_db),
) -> NotificationOut | None:
    """Tell someone they were mentioned on a page.

    The editor calls this when a mention is inserted. Mentioning yourself is
    allowed and silent; mentioning someone without access to the page is not.
    """
    if payload.user_id != access.user.id:
        target = db.get(User, payload.user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="No such user")
    row = await notify(
        db,
        registry.broadcaster,
        recipient_id=payload.user_id,
        actor=access.user,
        kind=NotificationKind.MENTION,
        document=access.document,
        snippet=payload.snippet,
    )
    if row is None and payload.user_id != access.user.id:
        raise HTTPException(status_code=400, detail="That person cannot open this page")
    return notification_out(row) if row else None
