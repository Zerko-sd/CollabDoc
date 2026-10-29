"""The writing assistant's endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..assistant import AssistantUnavailable, suggest
from ..config import get_settings
from ..deps import DocumentAccess, document_access
from ..models import Role
from ..ratelimit import RateLimiter, limiter
from ..realtime.hub import RoomRegistry
from ..schemas import AssistOut, AssistRequest
from .documents import get_registry, live_session

router = APIRouter(prefix="/api", tags=["assistant"])


@router.post("/documents/{document_id}/assist", response_model=AssistOut)
async def assist(
    payload: AssistRequest,
    request: Request,
    access: DocumentAccess = Depends(document_access),
    registry: RoomRegistry = Depends(get_registry),
    limits: RateLimiter = Depends(limiter),
) -> AssistOut:
    """Summarise the page, or improve, continue or outline it.

    Anyone who can read a page may summarise it; the actions that produce
    text to put into the page need edit access. Nothing is written here --
    the client inserts the suggestion as an ordinary, undoable edit.
    """
    if payload.action != "summarize":
        access.require(Role.EDITOR)
    limits.check("assist:%s" % access.user.id, get_settings().assist_rate_limit)
    session = await live_session(registry, access.document.id)
    page = session.content
    text = payload.text.strip() or page
    try:
        result = await suggest(payload.action, text, context=page)
    except AssistantUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return AssistOut(text=result.text, source=result.source, model=result.model)


@router.get("/assistant")
def assistant_status() -> dict:
    """Which assistant this server runs, so the client can label results."""
    provider = get_settings().ai_provider
    return {"provider": provider, "model": get_settings().ai_model if provider != "local" else None}
