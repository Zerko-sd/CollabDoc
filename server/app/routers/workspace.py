"""Comments, cross-document search, and editing activity.

The collaboration features that sit around the editor rather than inside it:
discussion threads anchored to passages, finding a page by what it says, and
a per-author timeline of who has been working on a document and when.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import (
    DocumentAccess,
    accessible_documents,
    document_access,
    get_current_user,
    require_editor,
)
from ..models import (
    AuditEvent,
    CommentReaction,
    Document,
    DocumentBlock,
    DocumentComment,
    DocumentOperation,
    NotificationKind,
    Role,
    User,
)
from ..notifications import mentioned_ids, notify
from ..realtime.hub import RoomRegistry
from ..realtime.versions import labelled_versions, same_version
from ..schemas import (
    ActivityAuthor,
    ActivityBucket,
    ActivityOut,
    ActivitySession,
    AuditEntry,
    CommentCreate,
    CommentOut,
    CommentUpdate,
    SearchHit,
    UserOut,
)
from .documents import get_registry

router = APIRouter(prefix="/api", tags=["workspace"])


# -- comments ---------------------------------------------------------------


REACTIONS = ("👍", "❤️", "🎉", "😄", "👀", "🚀")


def _reactions(db: Session, comment_ids: list[str]) -> dict[str, dict[str, list[str]]]:
    by_comment: dict[str, dict[str, list[str]]] = {}
    if not comment_ids:
        return by_comment
    for comment_id, user_id, emoji in db.execute(
        select(CommentReaction.comment_id, CommentReaction.user_id, CommentReaction.emoji)
        .where(CommentReaction.comment_id.in_(comment_ids))
        .order_by(CommentReaction.id)
    ):
        by_comment.setdefault(comment_id, {}).setdefault(emoji, []).append(user_id)
    # Always in the palette's order, so chips do not jump around.
    return {
        cid: {emoji: found[emoji] for emoji in REACTIONS if emoji in found}
        for cid, found in by_comment.items()
    }


def _comment_out(comment: DocumentComment, reactions: dict[str, list[str]] | None = None) -> CommentOut:
    return CommentOut(
        reactions=reactions or {},
        id=comment.id,
        document_id=comment.document_id,
        parent_id=comment.parent_id,
        anchor=comment.anchor,
        body=comment.body,
        resolved=comment.resolved,
        created_at=comment.created_at,
        user=UserOut.model_validate(comment.user),
    )


async def _announce(registry: RoomRegistry, document_id: str) -> None:
    """Tell everyone with the document open that its threads changed."""
    room = await registry.peek(document_id)
    if room is not None:
        await room.publish({"type": "comments", "document_id": document_id})


def _load_comment(db: Session, access: DocumentAccess, comment_id: str) -> DocumentComment:
    comment = db.get(DocumentComment, comment_id)
    if comment is None or comment.document_id != access.document.id:
        raise HTTPException(status_code=404, detail="Comment not found")
    return comment


@router.get("/documents/{document_id}/comments", response_model=list[CommentOut])
def list_comments(
    access: DocumentAccess = Depends(document_access), db: Session = Depends(get_db)
) -> list[CommentOut]:
    rows = db.scalars(
        select(DocumentComment)
        .where(DocumentComment.document_id == access.document.id)
        .order_by(DocumentComment.created_at)
    ).all()
    reactions = _reactions(db, [row.id for row in rows])
    return [_comment_out(row, reactions.get(row.id)) for row in rows]


@router.put(
    "/documents/{document_id}/comments/{comment_id}/reactions/{emoji}",
    response_model=CommentOut,
)
async def add_reaction(
    comment_id: str,
    emoji: str,
    access: DocumentAccess = Depends(document_access),
    registry: RoomRegistry = Depends(get_registry),
    db: Session = Depends(get_db),
) -> CommentOut:
    """React to a comment. Anyone who can read the page may; reacting twice
    with the same emoji is a no-op."""
    if emoji not in REACTIONS:
        raise HTTPException(status_code=422, detail="Pick one of %s" % " ".join(REACTIONS))
    comment = _load_comment(db, access, comment_id)
    exists = db.scalars(
        select(CommentReaction).where(
            CommentReaction.comment_id == comment.id,
            CommentReaction.user_id == access.user.id,
            CommentReaction.emoji == emoji,
        )
    ).first()
    if exists is None:
        db.add(CommentReaction(comment_id=comment.id, user_id=access.user.id, emoji=emoji))
        db.commit()
        await _announce(registry, access.document.id)
    return _comment_out(comment, _reactions(db, [comment.id]).get(comment.id))


@router.delete(
    "/documents/{document_id}/comments/{comment_id}/reactions/{emoji}",
    response_model=CommentOut,
)
async def remove_reaction(
    comment_id: str,
    emoji: str,
    access: DocumentAccess = Depends(document_access),
    registry: RoomRegistry = Depends(get_registry),
    db: Session = Depends(get_db),
) -> CommentOut:
    comment = _load_comment(db, access, comment_id)
    for row in db.scalars(
        select(CommentReaction).where(
            CommentReaction.comment_id == comment.id,
            CommentReaction.user_id == access.user.id,
            CommentReaction.emoji == emoji,
        )
    ):
        db.delete(row)
    db.commit()
    await _announce(registry, access.document.id)
    return _comment_out(comment, _reactions(db, [comment.id]).get(comment.id))


@router.post(
    "/documents/{document_id}/comments",
    response_model=CommentOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_comment(
    payload: CommentCreate,
    access: DocumentAccess = Depends(document_access),
    registry: RoomRegistry = Depends(get_registry),
    db: Session = Depends(get_db),
) -> CommentOut:
    """Start a thread or reply to one. Viewers may comment: discussing a page
    is not the same as changing it."""
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="A comment needs some text")
    anchor = payload.anchor.strip()
    if payload.parent_id is not None:
        parent = _load_comment(db, access, payload.parent_id)
        if parent.parent_id is not None:
            raise HTTPException(status_code=400, detail="Replies cannot be nested")
        anchor = parent.anchor
    comment = DocumentComment(
        document_id=access.document.id,
        user_id=access.user.id,
        parent_id=payload.parent_id,
        anchor=anchor,
        body=body,
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)
    await _announce(registry, access.document.id)

    told: set[str] = set()
    for user_id in mentioned_ids(body):
        if await notify(
            db,
            registry.broadcaster,
            recipient_id=user_id,
            actor=access.user,
            kind=NotificationKind.MENTION,
            document=access.document,
            snippet=body,
            comment_id=comment.id,
        ):
            told.add(user_id)
    if payload.parent_id is not None and parent.user_id not in told:
        await notify(
            db,
            registry.broadcaster,
            recipient_id=parent.user_id,
            actor=access.user,
            kind=NotificationKind.COMMENT_REPLY,
            document=access.document,
            snippet=body,
            comment_id=comment.id,
        )
    return _comment_out(comment)


@router.patch("/documents/{document_id}/comments/{comment_id}", response_model=CommentOut)
async def update_comment(
    comment_id: str,
    payload: CommentUpdate,
    access: DocumentAccess = Depends(document_access),
    registry: RoomRegistry = Depends(get_registry),
    db: Session = Depends(get_db),
) -> CommentOut:
    comment = _load_comment(db, access, comment_id)
    is_author = comment.user_id == access.user.id
    if payload.body is not None:
        if not is_author:
            raise HTTPException(status_code=403, detail="Only the author can edit a comment")
        comment.body = payload.body.strip()
    if payload.resolved is not None:
        # Resolving closes the thread for everyone, so it takes either the
        # thread's author or someone who can edit the document.
        if not (is_author or access.role.can(Role.EDITOR)):
            raise HTTPException(status_code=403, detail="Viewers can only resolve their own threads")
        if comment.parent_id is not None:
            raise HTTPException(status_code=400, detail="Resolve the thread, not a reply")
        comment.resolved = payload.resolved
    db.commit()
    db.refresh(comment)
    await _announce(registry, access.document.id)
    return _comment_out(comment)


@router.delete(
    "/documents/{document_id}/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_comment(
    comment_id: str,
    access: DocumentAccess = Depends(document_access),
    registry: RoomRegistry = Depends(get_registry),
    db: Session = Depends(get_db),
) -> None:
    comment = _load_comment(db, access, comment_id)
    if comment.user_id != access.user.id and access.role is not Role.OWNER:
        raise HTTPException(status_code=403, detail="Only the author or the owner can delete this")
    # Replies go with their thread; SQLite only cascades with foreign keys on,
    # so remove them explicitly rather than rely on the pragma.
    for reply in db.scalars(
        select(DocumentComment).where(DocumentComment.parent_id == comment.id)
    ).all():
        db.delete(reply)
    # Flush the replies first: with the pragma on, the database would cascade
    # them away under the parent's delete and the unit of work would miscount.
    db.flush()
    db.delete(comment)
    db.commit()
    await _announce(registry, access.document.id)


# -- search -----------------------------------------------------------------


def _snippet(text: str, needle: str, width: int = 90) -> str:
    at = text.lower().find(needle.lower())
    if at < 0:
        return text[:width]
    start = max(0, at - width // 3)
    end = min(len(text), start + width)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


@router.get("/search", response_model=list[SearchHit])
def search(
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[SearchHit]:
    """Pages the caller can open whose title or text mentions ``q``.

    Text matches come from the parsed block table, which the attribution pass
    keeps current, so search never has to replay an operation log.
    """
    needle = q.strip()
    if not needle:
        return []
    pattern = "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    roles: dict[str, Role] = {
        doc_id: role for doc_id, (_, role) in accessible_documents(db, user).items()
    }
    if not roles:
        return []

    matched_blocks = db.execute(
        select(DocumentBlock.document_id, DocumentBlock.content)
        .where(
            DocumentBlock.document_id.in_(roles),
            DocumentBlock.content.ilike(pattern, escape="\\"),
        )
        .order_by(DocumentBlock.sort_order)
    ).all()
    snippets: dict[str, str] = {}
    for doc_id, content in matched_blocks:
        snippets.setdefault(doc_id, _snippet(content, needle))

    documents = db.scalars(
        select(Document).where(
            Document.id.in_(roles),
            or_(Document.title.ilike(pattern, escape="\\"), Document.id.in_(snippets)),
        )
    ).all()

    def rank(document: Document) -> tuple:
        title = document.title.lower()
        return (
            not title.startswith(needle.lower()),
            needle.lower() not in title,
            -document.updated_at.timestamp(),
        )

    return [
        SearchHit(
            document_id=document.id,
            title=document.title,
            icon=document.icon,
            role=roles[document.id],
            snippet=snippets.get(document.id),
            updated_at=document.updated_at,
        )
        for document in sorted(documents, key=rank)[:limit]
    ]


# -- activity ---------------------------------------------------------------

# Bucket widths, smallest first; the first that fits the span in <= 48 bars wins.
_BUCKETS = [60, 300, 900, 3600, 6 * 3600, 86400, 7 * 86400]
# A session of a few quick keystrokes still took the author some time.
_MIN_SESSION_SECONDS = 30
_RECENT_SESSIONS = 20


def _aware(moment: datetime) -> datetime:
    # SQLite hands back naive datetimes; everything is stored in UTC.
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _changed(delta_json: str) -> tuple[int, int]:
    """Characters inserted and deleted by one operation."""
    inserted = deleted = 0
    for component in json.loads(delta_json):
        if isinstance(component, str):
            inserted += len(component)
        elif component < 0:
            deleted -= component
    return inserted, deleted


@router.get("/documents/{document_id}/activity", response_model=ActivityOut)
def activity(
    access: DocumentAccess = Depends(document_access), db: Session = Depends(get_db)
) -> ActivityOut:
    """Who edited, how much, and when -- the operation log, summarised.

    The log has a row per keystroke, which is the wrong unit for people. The
    chart measures characters changed, and edits are grouped into sessions
    exactly as the version history groups them (``realtime.versions``).
    """
    rows = db.execute(
        select(
            DocumentOperation.version,
            DocumentOperation.user_id,
            DocumentOperation.delta_json,
            DocumentOperation.timestamp,
        )
        .where(DocumentOperation.document_id == access.document.id)
        .order_by(DocumentOperation.version)
    ).all()
    if not rows:
        return ActivityOut(
            document_id=access.document.id,
            bucket_seconds=_BUCKETS[0],
            buckets=[],
            authors=[],
            sessions=[],
            first_edit=None,
            last_edit=None,
        )

    first = _aware(rows[0].timestamp)
    last = _aware(rows[-1].timestamp)
    span = max(1.0, (last - first).total_seconds())
    width = next((b for b in _BUCKETS if span / b <= 48), _BUCKETS[-1])
    origin = datetime.fromtimestamp(first.timestamp() // width * width, tz=timezone.utc)
    idle = timedelta(seconds=get_settings().version_group_idle_seconds)
    labels = labelled_versions(db, access.document.id)

    buckets: dict[int, dict[str, int]] = {}
    totals: dict[str, dict[str, int]] = {}
    sessions: list[dict] = []
    previous = None
    for version, user_id, delta_json, timestamp in rows:
        at = _aware(timestamp)
        inserted, deleted = _changed(delta_json)
        slot = int((at - origin).total_seconds() // width)
        counts = buckets.setdefault(slot, {})
        counts[user_id] = counts.get(user_id, 0) + inserted + deleted

        total = totals.setdefault(
            user_id, {"operations": 0, "inserted": 0, "deleted": 0, "sessions": 0, "active_seconds": 0}
        )
        total["operations"] += 1
        total["inserted"] += inserted
        total["deleted"] += deleted

        if previous is not None and same_version(
            previous[1], previous[2], previous[0] in labels, user_id, at, idle
        ):
            session = sessions[-1]
            session["ended_at"] = at
            session["inserted"] += inserted
            session["deleted"] += deleted
        else:
            sessions.append(
                {"user_id": user_id, "started_at": at, "ended_at": at, "inserted": inserted, "deleted": deleted}
            )
        previous = (version, user_id, at)

    for session in sessions:
        total = totals[session["user_id"]]
        total["sessions"] += 1
        total["active_seconds"] += max(
            _MIN_SESSION_SECONDS, int((session["ended_at"] - session["started_at"]).total_seconds())
        )

    people = {
        person.id: person
        for person in db.scalars(select(User).where(User.id.in_(totals))).all()
    }

    def identity(user_id: str) -> dict[str, str]:
        person = people.get(user_id)
        return {
            "name": person.name if person else "Unknown",
            "presence_color": person.presence_color if person else "#9b9a97",
        }

    authors = sorted(
        (
            ActivityAuthor(user_id=user_id, **identity(user_id), **counts)
            for user_id, counts in totals.items()
        ),
        key=lambda author: -author.inserted,
    )
    last_slot = max(buckets)
    return ActivityOut(
        document_id=access.document.id,
        bucket_seconds=width,
        buckets=[
            ActivityBucket(start=origin + timedelta(seconds=slot * width), counts=buckets.get(slot, {}))
            for slot in range(last_slot + 1)
        ],
        authors=authors,
        sessions=[
            ActivitySession(**session, **identity(session["user_id"]))
            for session in reversed(sessions[-_RECENT_SESSIONS:])
        ],
        first_edit=first,
        last_edit=last,
    )


# -- audit log --------------------------------------------------------------


@router.get("/documents/{document_id}/audit", response_model=list[AuditEntry])
def audit_log(
    limit: int = Query(default=100, ge=1, le=500),
    access: DocumentAccess = Depends(require_editor),
    db: Session = Depends(get_db),
) -> list[AuditEntry]:
    """Sharing and lifecycle changes, newest first.

    Viewers do not see it: it names everyone who was ever given access.
    """
    rows = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.document_id == access.document.id)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(limit)
    ).all()
    return [
        AuditEntry(
            action=row.action,
            detail=row.detail,
            actor=UserOut.model_validate(row.actor) if row.actor else None,
            created_at=row.created_at,
        )
        for row in rows
    ]
