"""Document lifecycle, saving, and version history (WBS 4.1 / 4.2)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..db import get_db
from ..deps import (
    MAX_DEPTH,
    DocumentAccess,
    accessible_documents,
    document_access,
    get_current_user,
    require_editor,
    require_owner,
    resolve_access,
    subtree_ids,
    trashed_access,
)
from ..models import (
    Document,
    DocumentCollaborator,
    DocumentOperation,
    DocumentSnapshot,
    Role,
    User,
)
from ..realtime.hub import RoomRegistry
from ..realtime.ot import TextOperation
from ..realtime.session import DocumentSession
from ..realtime.versions import labelled_versions, same_version
from ..schemas import (
    DocumentCreate,
    DocumentDetail,
    DocumentSaveRequest,
    DocumentSummary,
    DocumentUpdate,
    RevertRequest,
    VersionContent,
    VersionEntry,
)

router = APIRouter(prefix="/api/documents", tags=["documents"])


def get_registry(request: Request) -> RoomRegistry:
    return request.app.state.rooms


async def live_session(registry: RoomRegistry, document_id: str) -> DocumentSession:
    """The open session for a document, or a throwaway one loaded on demand.

    Reading a document must not leave a room (and its background workers)
    behind, so a request that arrives while nobody is editing gets its own
    short-lived session instead.
    """
    room = await registry.peek(document_id)
    if room is not None:
        return room.session
    session = DocumentSession(document_id, get_settings())
    await session.load()
    return session


def _summary(db: Session, document: Document, role: Role) -> DocumentSummary:
    count = db.scalar(
        select(func.count(DocumentCollaborator.id)).where(
            DocumentCollaborator.document_id == document.id
        )
    )
    return DocumentSummary(
        id=document.id,
        title=document.title,
        current_version=document.current_version,
        version_number=document.version_number,
        owner_id=document.owner_id,
        created_at=document.created_at,
        updated_at=document.updated_at,
        role=role,
        collaborator_count=(count or 0) + 1,  # collaborators plus the owner
        icon=document.icon,
        parent_id=document.parent_id,
        deleted_at=document.deleted_at,
        published=document.published,
        locked=document.locked,
    )


@router.get("", response_model=list[DocumentSummary])
def list_documents(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[DocumentSummary]:
    """Every page the caller can open, sub-pages of shared pages included.

    A page whose parent the caller cannot see is reported with no parent, so
    it shows at the top level of their sidebar instead of vanishing.
    """
    found = accessible_documents(db, user)
    summaries = []
    for document, role in found.values():
        summary = _summary(db, document, role)
        if summary.parent_id not in found:
            summary.parent_id = None
        summaries.append(summary)
    summaries.sort(key=lambda s: s.updated_at, reverse=True)
    return summaries


@router.get("/trash", response_model=list[DocumentSummary])
def list_trash(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[DocumentSummary]:
    """The caller's own pages in the trash, most recently deleted first.

    Only the page the user actually deleted is listed; the sub-pages that went
    with it come back when it is restored.
    """
    rows = db.scalars(
        select(Document)
        .where(Document.owner_id == user.id, Document.deleted_at.is_not(None))
        .order_by(Document.deleted_at.desc())
    ).all()
    trashed = {row.id for row in rows}
    return [_summary(db, row, Role.OWNER) for row in rows if row.parent_id not in trashed]


@router.post("", response_model=DocumentDetail, status_code=status.HTTP_201_CREATED)
async def create_document(
    payload: DocumentCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DocumentDetail:
    if payload.parent_id is not None:
        # Adding a page inside another one changes that page's tree.
        resolve_access(db, payload.parent_id, user).require(Role.EDITOR)
    document = Document(
        title=payload.title.strip() or "Untitled document",
        icon=(payload.icon or "").strip() or None,
        owner_id=user.id,
        parent_id=payload.parent_id,
    )
    db.add(document)
    db.flush()
    audit.record(db, document.id, user, "created", document.title)
    db.commit()
    db.refresh(document)

    if payload.content:
        # Seed text is recorded as an operation by the creator, so even the
        # first paragraph has an author in the ledger.
        session = DocumentSession(document.id, get_settings())
        await session.load()
        await session.submit(
            user.id, 0, TextOperation().insert(payload.content)
        )
        await session.flush()
        db.refresh(document)

    return DocumentDetail(
        **_summary(db, document, Role.OWNER).model_dump(),
        content=payload.content,
        share_token=document.share_token,
        share_role=document.share_role,
    )


@router.get("/{document_id}", response_model=DocumentDetail)
async def get_document(
    access: DocumentAccess = Depends(document_access),
    registry: RoomRegistry = Depends(get_registry),
    db: Session = Depends(get_db),
) -> DocumentDetail:
    session = await live_session(registry, access.document.id)
    detail = _summary(db, access.document, access.role).model_dump()
    detail["current_version"] = session.version
    detail["version_number"] = session.version_number
    return DocumentDetail(
        **detail,
        content=session.content,
        share_token=access.document.share_token if access.role is Role.OWNER else None,
        share_role=access.document.share_role,
    )


@router.patch("/{document_id}", response_model=DocumentSummary)
def rename_document(
    payload: DocumentUpdate,
    access: DocumentAccess = Depends(require_editor),
    db: Session = Depends(get_db),
) -> DocumentSummary:
    if payload.title is not None:
        title = payload.title.strip() or access.document.title
        if title != access.document.title:
            audit.record(db, access.document.id, access.user, "renamed",
                         "%s → %s" % (access.document.title, title))
        access.document.title = title
    if payload.icon is not None:
        access.document.icon = payload.icon.strip() or None
    if "parent_id" in payload.model_fields_set and payload.parent_id != access.document.parent_id:
        _move(db, access, payload.parent_id)
        parent = db.get(Document, payload.parent_id) if payload.parent_id else None
        audit.record(db, access.document.id, access.user, "moved",
                     "into %s" % parent.title if parent else "to the top level")
    db.commit()
    db.refresh(access.document)
    return _summary(db, access.document, access.role)


def _move(db: Session, access: DocumentAccess, parent_id: str | None) -> None:
    """Re-parent a page, refusing to put it inside itself."""
    if parent_id is None:
        access.document.parent_id = None
        return
    parent = resolve_access(db, parent_id, access.user)
    parent.require(Role.EDITOR)
    current = parent.document
    for _ in range(MAX_DEPTH):
        if current.id == access.document.id:
            raise HTTPException(status_code=400, detail="A page cannot be moved inside itself")
        if current.parent_id is None:
            break
        current = db.get(Document, current.parent_id)
        if current is None:
            break
    access.document.parent_id = parent_id


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    permanent: bool = Query(default=False),
    access: DocumentAccess = Depends(trashed_access),
    registry: RoomRegistry = Depends(get_registry),
    db: Session = Depends(get_db),
) -> None:
    """Move a page and its sub-pages to the trash, or with ``permanent``,
    delete them for good."""
    access.require(Role.OWNER)
    if not permanent and access.document.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Document not found")
    ids = subtree_ids(db, access.document.id)
    # Close the rooms first so no saver writes operations behind the change.
    for document_id in ids:
        await registry.force_close(document_id)
    if permanent:
        _delete_pages(db, ids)
    else:
        now = datetime.now(timezone.utc)
        for document in db.scalars(select(Document).where(Document.id.in_(ids))):
            if document.deleted_at is None:
                document.deleted_at = now
        audit.record(db, access.document.id, access.user, "trashed",
                     "with %d sub-page%s" % (len(ids) - 1, "" if len(ids) == 2 else "s") if len(ids) > 1 else "")
    db.commit()


def _delete_pages(db: Session, ids: list[str]) -> None:
    # Children first: an upgraded database has no cascading foreign key on
    # parent_id, and a fresh one would refuse to orphan them either way.
    for document_id in reversed(ids):
        document = db.get(Document, document_id)
        if document is not None:
            db.delete(document)
            db.flush()


@router.post("/{document_id}/restore", response_model=DocumentSummary)
def restore_document(
    access: DocumentAccess = Depends(trashed_access), db: Session = Depends(get_db)
) -> DocumentSummary:
    """Take a page, and the sub-pages trashed with it, back out of the trash."""
    access.require(Role.OWNER)
    if access.document.deleted_at is None:
        raise HTTPException(status_code=400, detail="That page is not in the trash")
    for document in db.scalars(
        select(Document).where(Document.id.in_(subtree_ids(db, access.document.id)))
    ):
        document.deleted_at = None
    parent = db.get(Document, access.document.parent_id) if access.document.parent_id else None
    if parent is None or parent.deleted_at is not None:
        # Its old home is gone or still in the trash: bring it back at the top.
        access.document.parent_id = None
    audit.record(db, access.document.id, access.user, "restored", "from the trash")
    db.commit()
    db.refresh(access.document)
    return _summary(db, access.document, access.role)


TRASH_RETENTION = timedelta(days=30)


def _aware(moment: datetime) -> datetime:
    # SQLite hands back naive datetimes; everything is stored in UTC.
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def purge_trash(db: Session) -> int:
    """Delete pages that have sat in the trash past the retention window."""
    cutoff = datetime.now(timezone.utc) - TRASH_RETENTION
    expired = [
        document.id
        for document in db.scalars(select(Document).where(Document.deleted_at.is_not(None)))
        if _aware(document.deleted_at) < cutoff
    ]
    removed = 0
    for document_id in expired:
        if db.get(Document, document_id) is None:
            continue  # already gone with an expired parent
        ids = subtree_ids(db, document_id)
        _delete_pages(db, ids)
        removed += len(ids)
    db.commit()
    return removed


@router.post("/{document_id}/save", response_model=DocumentDetail)
async def save_document(
    payload: DocumentSaveRequest,
    access: DocumentAccess = Depends(require_editor),
    registry: RoomRegistry = Depends(get_registry),
    db: Session = Depends(get_db),
) -> DocumentDetail:
    """Manual save (SRS 3.3).

    The client sends the buffer it has; the server diffs it against the
    authoritative text and records the difference as a normal operation, so a
    manual save merges with concurrent edits instead of overwriting them.
    """
    room = await registry.peek(access.document.id)
    session = room.session if room else await live_session(registry, access.document.id)
    operation = TextOperation.from_diff(session.content, payload.content)
    if not operation.is_noop():
        applied = await session.submit(access.user.id, session.version, operation)
        if room is not None:
            # People with the page open must receive the change too, or
            # their copies silently drift from the server's.
            await room.publish(
                {
                    "type": "op",
                    "version": applied.version,
                    "version_number": applied.version_number,
                    "operation": applied.operation.to_json(),
                    "user_id": access.user.id,
                }
            )
            room.signal_edit()
    await session.save_snapshot(payload.label)
    db.refresh(access.document)

    detail = _summary(db, access.document, access.role).model_dump()
    detail["current_version"] = session.version
    detail["version_number"] = session.version_number
    return DocumentDetail(
        **detail,
        content=session.content,
        share_token=access.document.share_token if access.role is Role.OWNER else None,
        share_role=access.document.share_role,
    )


@router.get("/{document_id}/versions", response_model=list[VersionEntry])
async def list_versions(
    access: DocumentAccess = Depends(document_access),
    registry: RoomRegistry = Depends(get_registry),
    db: Session = Depends(get_db),
    limit: int = 200,
) -> list[VersionEntry]:
    """Versions (editing bursts), newest first.

    The operation log keeps one row per keystroke because OT needs it; listing
    those rows directly buries the history in noise. See ``realtime.versions``
    for how operations are grouped.
    """
    session = await live_session(registry, access.document.id)
    await session.flush()
    limit = max(1, min(limit, 1000))
    idle = timedelta(seconds=get_settings().version_group_idle_seconds)
    labels = labelled_versions(db, access.document.id)
    rows = db.execute(
        select(DocumentOperation)
        .where(DocumentOperation.document_id == access.document.id)
        .order_by(DocumentOperation.version.desc())
        .execution_options(yield_per=500)
    ).scalars()

    bursts: list[dict] = []
    current: dict | None = None
    for row in rows:
        operation = TextOperation.from_json(row.delta_json)
        if current is not None and same_version(
            row.user_id,
            row.timestamp,
            row.version in labels,
            current["user_id"],
            current["started_at"],
            idle,
        ):
            # Walking newest to oldest, so this operation precedes the burst.
            current["delta"] = TextOperation.compose(operation, current["delta"])
            current["first_version"] = row.version
            current["operation_count"] += 1
            current["started_at"] = row.timestamp
            continue
        if len(bursts) == limit:
            break
        current = {
            "version": row.version,
            "number": session.version_number - len(bursts),
            "first_version": row.version,
            "operation_count": 1,
            "user_id": row.user_id,
            "delta": operation,
            "started_at": row.timestamp,
            "timestamp": row.timestamp,
            "label": labels.get(row.version),
        }
        bursts.append(current)

    names = {
        user_id: db.scalar(select(User.name).where(User.id == user_id))
        for user_id in {burst["user_id"] for burst in bursts}
    }
    return [
        VersionEntry(
            **{**burst, "delta": burst["delta"].to_json()},
            user_name=names.get(burst["user_id"]),
        )
        for burst in bursts
    ]


@router.get("/{document_id}/versions/{version}", response_model=VersionContent)
async def get_version(
    version: int,
    access: DocumentAccess = Depends(document_access),
    registry: RoomRegistry = Depends(get_registry),
) -> VersionContent:
    session = await live_session(registry, access.document.id)
    await session.flush()
    try:
        content = session.content_at(version)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return VersionContent(version=version, content=content)


@router.post("/{document_id}/revert", response_model=DocumentDetail)
async def revert_document(
    payload: RevertRequest,
    access: DocumentAccess = Depends(require_editor),
    registry: RoomRegistry = Depends(get_registry),
    db: Session = Depends(get_db),
) -> DocumentDetail:
    """Roll back to an earlier version by appending the operation that undoes it."""
    room = await registry.peek(access.document.id)
    session = room.session if room else await live_session(registry, access.document.id)
    await session.flush()
    try:
        applied = await session.revert(access.user.id, payload.version)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.flush()
    audit.record(db, access.document.id, access.user, "reverted", "to revision %d" % payload.version)
    db.commit()

    if room is not None:
        # Everyone with the document open needs the undoing operation too.
        await room.publish(
            {
                "type": "op",
                "version": applied.version,
                "version_number": applied.version_number,
                "operation": applied.operation.to_json(),
                "user_id": access.user.id,
            }
        )
        room.signal_edit()

    db.refresh(access.document)
    detail = _summary(db, access.document, access.role).model_dump()
    detail["current_version"] = session.version
    detail["version_number"] = session.version_number
    return DocumentDetail(
        **detail,
        content=session.content,
        share_token=access.document.share_token if access.role is Role.OWNER else None,
        share_role=access.document.share_role,
    )


@router.get("/{document_id}/snapshots")
async def list_snapshots(
    access: DocumentAccess = Depends(document_access),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Named save points, newest first."""
    rows = db.scalars(
        select(DocumentSnapshot)
        .where(DocumentSnapshot.document_id == access.document.id)
        .order_by(DocumentSnapshot.version.desc())
    ).all()
    return [
        {
            "version": row.version,
            "label": row.label,
            "created_at": row.created_at.isoformat(),
            "size": len(row.content),
        }
        for row in rows
    ]
