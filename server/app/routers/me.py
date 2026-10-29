"""Per-user preferences that should follow someone between devices."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import accessible_documents, get_current_user
from ..models import User, UserFavorite
from ..schemas import FavoritesUpdate

router = APIRouter(prefix="/api/me", tags=["me"])


def _favorites(db: Session, user: User) -> list[str]:
    """Favourite page ids in sidebar order, minus any the user can no longer open."""
    visible = accessible_documents(db, user)
    rows = db.scalars(
        select(UserFavorite.document_id)
        .where(UserFavorite.user_id == user.id)
        .order_by(UserFavorite.position)
    )
    return [document_id for document_id in rows if document_id in visible]


@router.get("/favorites", response_model=list[str])
def get_favorites(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[str]:
    return _favorites(db, user)


@router.put("/favorites", response_model=list[str])
def set_favorites(
    payload: FavoritesUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[str]:
    """Replace the whole list; the order sent is the order shown."""
    visible = accessible_documents(db, user)
    wanted = [doc_id for doc_id in dict.fromkeys(payload.document_ids) if doc_id in visible]
    db.execute(delete(UserFavorite).where(UserFavorite.user_id == user.id))
    db.add_all(
        UserFavorite(user_id=user.id, document_id=doc_id, position=index)
        for index, doc_id in enumerate(wanted)
    )
    db.commit()
    return wanted
