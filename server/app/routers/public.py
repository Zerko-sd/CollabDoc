"""Pages published to the web: readable by anyone with the link, no sign-in."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Attachment, Document
from ..publish import render_page
from .documents import get_registry, live_session
from .files import serve

router = APIRouter(tags=["public"], include_in_schema=False)


def _published(db: Session, document_id: str) -> Document:
    document = db.get(Document, document_id)
    if document is None or not document.published or document.deleted_at is not None:
        raise HTTPException(status_code=404, detail="This page is not published")
    return document


@router.get("/p/{document_id}", response_class=HTMLResponse)
async def published_page(
    document_id: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    document = _published(db, document_id)
    session = await live_session(get_registry(request), document.id)
    return HTMLResponse(
        render_page(document.title, document.icon, session.content, document.id),
        headers={
            # The page is static HTML: no script runs, nothing embeds it.
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
            "X-Frame-Options": "DENY",
        },
    )


@router.get("/p/{document_id}/files/{file_id}", response_class=Response)
def published_file(
    document_id: str, file_id: str, db: Session = Depends(get_db)
) -> Response:
    document = _published(db, document_id)
    row = db.get(Attachment, file_id)
    if row is None or row.document_id != document.id:
        raise HTTPException(status_code=404, detail="File not found")
    return serve(row)
