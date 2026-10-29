"""Telling people about things other people did: mentions, replies, invites.

A notification is a row for the inbox plus a message on the recipient's own
broadcaster channel, so an open tab hears about it at once -- the same
publish/subscribe round trip the document rooms use, keyed by user instead of
by document.
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from .deps import role_on
from .models import Document, Notification, NotificationKind, User
from .realtime.broadcaster import Broadcaster
from .schemas import NotificationOut, UserOut

# How a mention sits in the text: ``@[Ada Lovelace](user:<uuid>)``.
MENTION = re.compile(r"@\[([^\]\n]+)\]\(user:([0-9a-f-]{36})\)")


def user_channel(user_id: str) -> str:
    return "user:%s" % user_id


def mentioned_ids(text: str) -> list[str]:
    """User ids mentioned in ``text``, first mention first, without repeats."""
    return list(dict.fromkeys(match.group(2) for match in MENTION.finditer(text)))


def plain(text: str, limit: int = 280) -> str:
    """Text for a notification preview: mentions shown as ``@Name``."""
    flat = " ".join(MENTION.sub(lambda m: "@" + m.group(1), text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def notification_out(row: Notification) -> NotificationOut:
    return NotificationOut(
        id=row.id,
        kind=row.kind,
        document_id=row.document_id,
        document_title=row.document.title,
        document_icon=row.document.icon,
        comment_id=row.comment_id,
        snippet=row.snippet,
        actor=UserOut.model_validate(row.actor),
        read=row.read_at is not None,
        created_at=row.created_at,
    )


async def notify(
    db: Session,
    broadcaster: Broadcaster,
    *,
    recipient_id: str,
    actor: User,
    kind: NotificationKind,
    document: Document,
    snippet: str = "",
    comment_id: str | None = None,
) -> Notification | None:
    """Record a notification and push it to the recipient's open tabs.

    Nobody is notified about their own action, nor about a page they cannot
    open -- a mention must not leak a page to someone without access.
    """
    if recipient_id == actor.id:
        return None
    recipient = db.get(User, recipient_id)
    if recipient is None or role_on(db, document, recipient) is None:
        return None
    row = Notification(
        user_id=recipient_id,
        actor_id=actor.id,
        kind=kind,
        document_id=document.id,
        comment_id=comment_id,
        snippet=plain(snippet),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    await broadcaster.publish(
        user_channel(recipient_id),
        {"type": "notification", "notification": notification_out(row).model_dump(mode="json")},
    )
    return row
