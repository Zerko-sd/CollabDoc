"""Authorship attribution endpoints (WBS 6.3, 9.3)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import DocumentAccess, document_access, require_editor
from ..models import AuthorSample
from ..realtime.hub import RoomRegistry
from ..schemas import AttributionMetrics, AttributionOut, BlockOut
from .documents import get_registry, live_session

router = APIRouter(prefix="/api/documents", tags=["attribution"])


async def _attributed(registry: RoomRegistry, document_id: str, force: bool):
    session = await live_session(registry, document_id)
    blocks = await session.refresh_attribution(force=force)
    return session, blocks


@router.get("/{document_id}/attribution", response_model=AttributionOut)
async def get_attribution(
    access: DocumentAccess = Depends(document_access),
    registry: RoomRegistry = Depends(get_registry),
    refresh: bool = False,
) -> AttributionOut:
    """Who wrote each block, and how sure the engine is."""
    session, blocks = await _attributed(registry, access.document.id, refresh)
    return AttributionOut(
        document_id=access.document.id,
        version=session.version,
        blocks=[BlockOut(**block.to_dict()) for block in blocks],
        model=session.engine.report.to_dict(),
    )


@router.post("/{document_id}/attribution/refresh", response_model=AttributionOut)
async def refresh_attribution(
    access: DocumentAccess = Depends(require_editor),
    registry: RoomRegistry = Depends(get_registry),
) -> AttributionOut:
    """Retrain the stylometric model and re-attribute every block."""
    session, blocks = await _attributed(registry, access.document.id, True)
    return AttributionOut(
        document_id=access.document.id,
        version=session.version,
        blocks=[BlockOut(**block.to_dict()) for block in blocks],
        model=session.engine.report.to_dict(),
    )


@router.get("/{document_id}/attribution/metrics", response_model=AttributionMetrics)
async def attribution_metrics(
    access: DocumentAccess = Depends(document_access),
    registry: RoomRegistry = Depends(get_registry),
) -> AttributionMetrics:
    """Quality readout for the attribution pass (WBS 9.3).

    ``agreement_rate`` is how often the stylometric model independently picks
    the same author the keystroke ledger recorded. It is the honest measure of
    the model here: the ledger is the closest thing to ground truth that exists
    at runtime, so agreement is what a test report can be signed off against.
    """
    session, blocks = await _attributed(registry, access.document.id, False)
    hybrid = [b for b in blocks if b.method == "hybrid"]
    agree = sum(
        1 for b in hybrid if b.style_user_id and b.style_user_id == b.ledger_user_id
    )

    per_author: dict[str, dict[str, float]] = {}
    for block in blocks:
        if block.attributed_user_id is None:
            continue
        entry = per_author.setdefault(
            block.attributed_user_id, {"blocks": 0.0, "characters": 0.0, "mean_confidence": 0.0}
        )
        entry["blocks"] += 1
        entry["characters"] += len(block.content)
        entry["mean_confidence"] += block.confidence_score
    for entry in per_author.values():
        entry["mean_confidence"] = round(entry["mean_confidence"] / entry["blocks"], 4)

    return AttributionMetrics(
        document_id=access.document.id,
        version=session.version,
        model=session.engine.report.to_dict(),
        block_count=len(blocks),
        mean_confidence=(
            round(sum(b.confidence_score for b in blocks) / len(blocks), 4)
            if blocks
            else 0.0
        ),
        agreement_rate=round(agree / len(hybrid), 4) if hybrid else 0.0,
        per_author=per_author,
    )


@router.post("/{document_id}/attribution/samples", status_code=201)
async def add_writing_sample(
    payload: dict,
    access: DocumentAccess = Depends(require_editor),
    db: Session = Depends(get_db),
) -> dict:
    """Contribute a known-authorship writing sample for the caller.

    Lets a user prime the model with prose they wrote elsewhere, so
    attribution is useful before they have typed enough in this document for
    their style to separate from everyone else's.
    """
    content = str(payload.get("content", "")).strip()
    if not content:
        return {"stored": 0}
    sample = AuthorSample(
        document_id=access.document.id, user_id=access.user.id, content=content
    )
    db.add(sample)
    db.commit()
    return {"stored": 1, "characters": len(content)}
