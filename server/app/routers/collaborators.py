"""Sharing and permission management (WBS 3.2 / 4.2, SRS 2.2)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import MAX_DEPTH, DocumentAccess, document_access, get_current_user, inherited, require_owner
from ..models import Document, DocumentCollaborator, NotificationKind, Role, User
from ..notifications import notify
from ..realtime.hub import RoomRegistry
from ..schemas import (
    CollaboratorAdd,
    CollaboratorOut,
    CollaboratorRoleUpdate,
    PublishOut,
    PublishRequest,
    ShareLinkOut,
    ShareLinkRequest,
    UserOut,
)
from ..security import new_share_token

router = APIRouter(prefix="/api/documents", tags=["collaborators"])


@router.get("/{document_id}/collaborators", response_model=list[CollaboratorOut])
def list_collaborators(
    access: DocumentAccess = Depends(document_access), db: Session = Depends(get_db)
) -> list[CollaboratorOut]:
    owner = db.get(User, access.document.owner_id)
    people = [CollaboratorOut(user=UserOut.model_validate(owner), role=Role.OWNER)]
    grants = db.scalars(
        select(DocumentCollaborator).where(
            DocumentCollaborator.document_id == access.document.id
        )
    ).all()
    people.extend(
        CollaboratorOut(user=UserOut.model_validate(grant.user), role=grant.role)
        for grant in grants
    )
    # People who reach this page through a parent it is nested in.
    seen = {person.user.id for person in people}
    parent_id = access.document.parent_id
    for _ in range(MAX_DEPTH):
        parent = db.get(Document, parent_id) if parent_id else None
        if parent is None:
            break
        holders = [(db.get(User, parent.owner_id), Role.OWNER)] + [
            (grant.user, grant.role)
            for grant in db.scalars(
                select(DocumentCollaborator).where(DocumentCollaborator.document_id == parent.id)
            )
        ]
        for user, role in holders:
            if user is not None and user.id not in seen:
                seen.add(user.id)
                people.append(
                    CollaboratorOut(
                        user=UserOut.model_validate(user), role=inherited(role), inherited=True
                    )
                )
        parent_id = parent.parent_id
    return people


@router.post(
    "/{document_id}/collaborators",
    response_model=CollaboratorOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_collaborator(
    payload: CollaboratorAdd,
    request: Request,
    access: DocumentAccess = Depends(require_owner),
    db: Session = Depends(get_db),
) -> CollaboratorOut:
    """Invite a registered user directly (SRS 2.2 Document Sharing)."""
    invitee = db.scalars(select(User).where(User.email == payload.email.lower())).first()
    if invitee is None:
        raise HTTPException(status_code=404, detail="No user with that email")
    if invitee.id == access.document.owner_id:
        raise HTTPException(status_code=409, detail="That user already owns this document")
    if payload.role is Role.OWNER:
        raise HTTPException(status_code=400, detail="Ownership cannot be granted this way")

    grant = db.scalars(
        select(DocumentCollaborator).where(
            DocumentCollaborator.document_id == access.document.id,
            DocumentCollaborator.user_id == invitee.id,
        )
    ).first()
    is_new = grant is None
    if grant is None:
        grant = DocumentCollaborator(
            document_id=access.document.id, user_id=invitee.id, role=payload.role
        )
        db.add(grant)
        audit.record(db, access.document.id, access.user, "invited",
                     "%s as %s" % (invitee.email, payload.role.value.lower()))
    else:
        if grant.role is not payload.role:
            audit.record(db, access.document.id, access.user, "role_changed",
                         "%s → %s" % (invitee.email, payload.role.value.lower()))
        grant.role = payload.role
    db.commit()
    if is_new:
        registry: RoomRegistry = request.app.state.rooms
        await notify(
            db,
            registry.broadcaster,
            recipient_id=invitee.id,
            actor=access.user,
            kind=NotificationKind.SHARED,
            document=access.document,
            snippet="Invited you as %s" % payload.role.value.lower(),
        )
    return CollaboratorOut(user=UserOut.model_validate(invitee), role=payload.role)


@router.patch(
    "/{document_id}/collaborators/{user_id}", response_model=CollaboratorOut
)
def update_collaborator_role(
    user_id: str,
    payload: CollaboratorRoleUpdate,
    access: DocumentAccess = Depends(require_owner),
    db: Session = Depends(get_db),
) -> CollaboratorOut:
    if payload.role is Role.OWNER:
        raise HTTPException(status_code=400, detail="Ownership cannot be granted this way")
    grant = db.scalars(
        select(DocumentCollaborator).where(
            DocumentCollaborator.document_id == access.document.id,
            DocumentCollaborator.user_id == user_id,
        )
    ).first()
    if grant is None:
        raise HTTPException(status_code=404, detail="That user is not a collaborator")
    if grant.role is not payload.role:
        audit.record(db, access.document.id, access.user, "role_changed",
                     "%s → %s" % (grant.user.email, payload.role.value.lower()))
    grant.role = payload.role
    db.commit()
    return CollaboratorOut(user=UserOut.model_validate(grant.user), role=grant.role)


@router.delete(
    "/{document_id}/collaborators/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
def remove_collaborator(
    user_id: str,
    access: DocumentAccess = Depends(require_owner),
    db: Session = Depends(get_db),
) -> None:
    grant = db.scalars(
        select(DocumentCollaborator).where(
            DocumentCollaborator.document_id == access.document.id,
            DocumentCollaborator.user_id == user_id,
        )
    ).first()
    if grant is None:
        raise HTTPException(status_code=404, detail="That user is not a collaborator")
    audit.record(db, access.document.id, access.user, "removed", grant.user.email)
    db.delete(grant)
    db.commit()


@router.put("/{document_id}/share", response_model=ShareLinkOut)
def set_share_link(
    payload: ShareLinkRequest,
    request: Request,
    access: DocumentAccess = Depends(require_owner),
    db: Session = Depends(get_db),
) -> ShareLinkOut:
    """Turn link sharing on at a given role, or off entirely."""
    if payload.role is Role.OWNER:
        raise HTTPException(status_code=400, detail="A link cannot grant ownership")
    audit.record(db, access.document.id, access.user, "link_sharing",
                 "anyone with the link can %s" % ("edit" if payload.role is Role.EDITOR else "view")
                 if payload.role else "turned off")
    access.document.share_role = payload.role
    if payload.role is not None:
        # A fresh token on every change means revoking access really revokes
        # it -- an old link cannot be re-enabled by flipping the setting back.
        access.document.share_token = new_share_token()
    db.commit()
    db.refresh(access.document)
    return _share_link(request, access.document)


@router.get("/{document_id}/share", response_model=ShareLinkOut)
def get_share_link(
    request: Request, access: DocumentAccess = Depends(require_owner)
) -> ShareLinkOut:
    return _share_link(request, access.document)


def _share_link(request: Request, document: Document) -> ShareLinkOut:
    if document.share_role is None:
        return ShareLinkOut(share_token=None, share_role=None, url=None)
    base = str(request.base_url).rstrip("/")
    return ShareLinkOut(
        share_token=document.share_token,
        share_role=document.share_role,
        url="%s/?join=%s" % (base, document.share_token),
    )


@router.post("/join/{share_token}", response_model=CollaboratorOut)
def join_with_link(
    share_token: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CollaboratorOut:
    """Claim access to a document from a share link."""
    document = db.scalars(
        select(Document).where(Document.share_token == share_token)
    ).first()
    if document is None or document.share_role is None:
        raise HTTPException(status_code=404, detail="That share link is not valid")
    if document.owner_id == user.id:
        return CollaboratorOut(user=UserOut.model_validate(user), role=Role.OWNER)

    grant = db.scalars(
        select(DocumentCollaborator).where(
            DocumentCollaborator.document_id == document.id,
            DocumentCollaborator.user_id == user.id,
        )
    ).first()
    if grant is None:
        grant = DocumentCollaborator(
            document_id=document.id, user_id=user.id, role=document.share_role
        )
        db.add(grant)
        audit.record(db, document.id, user, "joined_by_link", document.share_role.value.lower())
        db.commit()
    elif grant.role.rank < document.share_role.rank:
        # A link upgrades a weaker existing grant but never downgrades one.
        grant.role = document.share_role
        db.commit()
    return CollaboratorOut(user=UserOut.model_validate(user), role=grant.role)


@router.put("/{document_id}/publish", response_model=PublishOut)
def set_published(
    payload: PublishRequest,
    request: Request,
    access: DocumentAccess = Depends(require_owner),
    db: Session = Depends(get_db),
) -> PublishOut:
    """Publish a read-only copy of the page to the web, or take it down."""
    if access.document.published != payload.published:
        audit.record(db, access.document.id, access.user,
                     "published" if payload.published else "unpublished", "")
    access.document.published = payload.published
    db.commit()
    return _publish_out(request, access.document)


@router.get("/{document_id}/publish", response_model=PublishOut)
def get_published(
    request: Request, access: DocumentAccess = Depends(require_owner)
) -> PublishOut:
    return _publish_out(request, access.document)


def _publish_out(request: Request, document: Document) -> PublishOut:
    if not document.published:
        return PublishOut(published=False, url=None)
    base = str(request.base_url).rstrip("/")
    return PublishOut(published=True, url="%s/p/%s" % (base, document.id))
