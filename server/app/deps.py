"""Shared dependencies: authentication and role checks (WBS 3.1 / 3.2)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import ApiToken, Document, DocumentCollaborator, Role, User
from .security import decode_access_token


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token:
        return token
    # The WebSocket handshake cannot carry an Authorization header from the
    # browser API, so the token may arrive as a query parameter instead.
    return request.query_params.get("token")


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    token = _bearer_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if token.startswith(API_TOKEN_PREFIX):
        return _token_user(db, token)
    user_id = decode_access_token(token)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown user"
        )
    return user


API_TOKEN_PREFIX = "cd_"


def hash_api_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _token_user(db: Session, token: str) -> User:
    """The owner of a personal access token, if it exists and is not revoked."""
    row = db.scalars(select(ApiToken).where(ApiToken.token_hash == hash_api_token(token))).first()
    if row is None or row.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    now = datetime.now(timezone.utc)
    last = row.last_used_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    # Record use at most once a minute, so a busy script is not a write storm.
    if last is None or now - last > timedelta(minutes=1):
        row.last_used_at = now
        db.commit()
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown user")
    return user


@dataclass
class DocumentAccess:
    """A document together with the role the caller holds on it."""

    document: Document
    user: User
    role: Role

    def require(self, minimum: Role) -> None:
        if not self.role.can(minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This action requires the %s role; you have %s."
                % (minimum.value, self.role.value),
            )


def resolve_access(
    db: Session, document_id: str, user: User, *, include_trashed: bool = False
) -> DocumentAccess:
    """Look up the caller's role, treating a missing grant as a missing document.

    Answering 404 rather than 403 for a document the caller has no grant on
    keeps the API from confirming that a document id exists (SRS 5.3). A page
    in the trash is missing too, except to the routes that manage the trash.
    """
    document = db.get(Document, document_id)
    if document is None or (document.deleted_at is not None and not include_trashed):
        raise HTTPException(status_code=404, detail="Document not found")
    role = role_on(db, document, user)
    if role is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return DocumentAccess(document, user, locked(document, role))


def locked(document: Document, role: Role) -> Role:
    """A locked page is read-only for everyone except its owner."""
    return Role.VIEWER if document.locked and role is not Role.OWNER else role


# Deepest page nesting the access walk will follow; also bounds a corrupt cycle.
MAX_DEPTH = 32


def _direct_role(db: Session, document: Document, user: User) -> Role | None:
    if document.owner_id == user.id:
        return Role.OWNER
    return db.scalar(
        select(DocumentCollaborator.role).where(
            DocumentCollaborator.document_id == document.id,
            DocumentCollaborator.user_id == user.id,
        )
    )


def inherited(role: Role) -> Role:
    """What a role on a parent page grants on the pages beneath it.

    Sharing a page shares everything nested in it, but owning a parent does
    not make you the owner of somebody else's sub-page: deleting it and
    managing its sharing stay with the person who created it.
    """
    return Role.EDITOR if role is Role.OWNER else role


def role_on(db: Session, document: Document, user: User) -> Role | None:
    """The caller's role on a page: their own grant, else the nearest ancestor's."""
    role = _direct_role(db, document, user)
    if role is not None:
        return role
    current = document
    for _ in range(MAX_DEPTH):
        if current.parent_id is None:
            return None
        current = db.get(Document, current.parent_id)
        if current is None:
            return None
        role = _direct_role(db, current, user)
        if role is not None:
            return inherited(role)
    return None


def accessible_documents(db: Session, user: User) -> dict[str, tuple[Document, Role]]:
    """Every live page the caller can open, with the role they hold on it.

    Starts from the pages they own or were invited to, then walks down the
    page tree, so a shared parent brings its sub-pages along.
    """
    found: dict[str, tuple[Document, Role]] = {}
    for document in db.scalars(
        select(Document).where(Document.owner_id == user.id, Document.deleted_at.is_(None))
    ):
        found[document.id] = (document, Role.OWNER)
    for document, role in db.execute(
        select(Document, DocumentCollaborator.role)
        .join(DocumentCollaborator, DocumentCollaborator.document_id == Document.id)
        .where(DocumentCollaborator.user_id == user.id, Document.deleted_at.is_(None))
    ):
        found.setdefault(document.id, (document, role))

    frontier = list(found)
    for _ in range(MAX_DEPTH):
        if not frontier:
            break
        children = db.scalars(
            select(Document).where(
                Document.parent_id.in_(frontier), Document.deleted_at.is_(None)
            )
        ).all()
        frontier = []
        for child in children:
            if child.id not in found:
                found[child.id] = (child, inherited(found[child.parent_id][1]))
                frontier.append(child.id)
    return {key: (document, locked(document, role)) for key, (document, role) in found.items()}


def subtree_ids(db: Session, root_id: str) -> list[str]:
    """A page and everything nested under it, parents before children."""
    ids = [root_id]
    frontier = [root_id]
    for _ in range(MAX_DEPTH):
        if not frontier:
            break
        frontier = [
            child_id
            for child_id in db.scalars(select(Document.id).where(Document.parent_id.in_(frontier)))
            if child_id not in ids
        ]
        ids.extend(frontier)
    return ids


def document_access(
    document_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DocumentAccess:
    return resolve_access(db, document_id, user)


def trashed_access(
    document_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DocumentAccess:
    """Access that also reaches pages in the trash (restore, delete forever).

    The trash is the owner's: to anyone else a trashed page stays missing.
    """
    access = resolve_access(db, document_id, user, include_trashed=True)
    if access.document.deleted_at is not None and access.role is not Role.OWNER:
        raise HTTPException(status_code=404, detail="Document not found")
    return access


def require_editor(access: DocumentAccess = Depends(document_access)) -> DocumentAccess:
    access.require(Role.EDITOR)
    return access


def require_owner(access: DocumentAccess = Depends(document_access)) -> DocumentAccess:
    access.require(Role.OWNER)
    return access
