"""The academic integrity report: who really wrote this page, and what to look at.

Everything here is read from evidence the server already keeps -- nothing is
asked of the writers and nothing new is stored:

* the **operation log** says who typed what and when, so it gives each
  person's effort (characters typed and deleted, time spent, sessions) and
  shows text that arrived all at once rather than keystroke by keystroke;
* the **keystroke ledger** says who typed each character still on the page,
  so it gives each person's share of the final text;
* the **attribution pass** says whose writing each block *reads like*.

The flags are where those disagree. A block one person typed that reads like
another's style is the classic sign of a copied passage, and a large paste
is text whose real origin the server cannot see. Neither proves anything on
its own -- the report says so -- but together they tell a reviewer where to
look first.

The style check is leave-one-out. The live attribution pass trains on what
the ledger says each person typed, so a passage Bob copied from Alice is in
*Bob's* training data and pulls the model towards Bob -- the copy hides
itself. Here every block is judged by a model retrained without that block,
which asks the honest question: does this read like Bob's *other* writing?
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import DocumentAccess, document_access
from ..attribution import blocks as block_parser
from ..attribution.engine import AttributionEngine
from ..attribution.trail import AuthorTrail
from ..models import AuthorSample, DocumentOperation, User
from ..realtime.hub import RoomRegistry
from ..realtime.ot import TextOperation, is_delete, is_insert
from .documents import get_registry, live_session

router = APIRouter(prefix="/api/documents", tags=["integrity"])

# One operation inserting at least this much text was not typed: browsers
# send keystrokes, and even a slow link composes only a few into one frame.
PASTE_MIN_CHARS = 120
# Style has to be fairly sure, and have enough prose to judge, before a
# disagreement with the ledger is worth a reviewer's time.
MISMATCH_MIN_CONFIDENCE = 0.6
MISMATCH_MIN_CHARS = 60
# Gaps between one person's operations: shorter counts as working time,
# longer starts a new writing session.
ACTIVE_GAP = timedelta(minutes=5)
SESSION_GAP = timedelta(minutes=30)
# Each leave-one-out check retrains the model, which takes milliseconds;
# the longest blocks carry the most style and are checked first.
MAX_CHECKED_BLOCKS = 40


class Contributor(BaseModel):
    user_id: str
    name: str
    color: str
    operations: int
    typed: int  # characters inserted keystroke by keystroke
    pasted: int  # characters inserted in large single operations
    deleted: int
    surviving: int  # characters on the page now that this person typed
    share: float  # surviving / page length
    sessions: int
    active_minutes: float
    first_edit: datetime | None
    last_edit: datetime | None


class Flag(BaseModel):
    kind: str  # "style_mismatch" | "paste"
    severity: str  # "high" | "medium" | "low"
    user_id: str | None
    other_user_id: str | None = None  # style_mismatch: whose style it reads like
    title: str
    detail: str
    snippet: str
    start: int | None = None  # position on the page, when it is still there
    end: int | None = None
    version: int | None = None
    timestamp: datetime | None = None


class IntegrityReport(BaseModel):
    document_id: str
    title: str
    version: int
    generated_at: datetime
    characters: int
    keystroke_share: float  # of everything inserted, how much was typed
    agreement_rate: float  # leave-one-out style agreeing with the ledger
    judged_blocks: int  # blocks the leave-one-out check could judge
    model: dict
    contributors: list[Contributor]
    flags: list[Flag]


def _aware(moment: datetime) -> datetime:
    # SQLite hands timestamps back naive; they were written in UTC.
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _snippet(text: str, width: int = 140) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1].rstrip() + "…"


@router.get("/{document_id}/integrity", response_model=IntegrityReport)
async def integrity_report(
    access: DocumentAccess = Depends(document_access),
    registry: RoomRegistry = Depends(get_registry),
    db: Session = Depends(get_db),
) -> IntegrityReport:
    """Contribution and authenticity evidence for one page."""
    session = await live_session(registry, access.document.id)
    await session.flush()
    await session.refresh_attribution(force=False)
    content = session.content

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

    # -- effort, from the operation log ------------------------------------
    stats: dict[str, dict] = {}
    times: dict[str, list[datetime]] = {}
    flags: list[Flag] = []
    for row in rows:
        entry = stats.setdefault(
            row.user_id, {"operations": 0, "typed": 0, "pasted": 0, "deleted": 0}
        )
        entry["operations"] += 1
        times.setdefault(row.user_id, []).append(_aware(row.timestamp))
        for component in json.loads(row.delta_json):
            if is_insert(component):
                if len(component) >= PASTE_MIN_CHARS:
                    entry["pasted"] += len(component)
                    flags.append(_paste_flag(row, component, content))
                else:
                    entry["typed"] += len(component)
            elif is_delete(component):
                entry["deleted"] += -component

    # -- share of the final text, from the ledger ---------------------------
    surviving: dict[str, int] = {}
    for size, user in session.trail.runs:
        if user is not None:
            surviving[user] = surviving.get(user, 0) + size

    # -- where style and keystrokes disagree (leave-one-out) -------------
    samples: dict[str, list[str]] = {}
    for sample in db.scalars(
        select(AuthorSample).where(AuthorSample.user_id.in_(set(surviving) or {""}))
    ).all():
        samples.setdefault(sample.user_id, []).append(sample.content)
    verdicts = await asyncio.to_thread(
        _leave_one_out, content, session.trail.to_json(), samples, session.engine
    )
    agree = sum(1 for v in verdicts if v["style_user_id"] == v["ledger_user_id"])
    for verdict in verdicts:
        if (
            verdict["style_user_id"] != verdict["ledger_user_id"]
            and verdict["style_confidence"] >= MISMATCH_MIN_CONFIDENCE
        ):
            paste = next(
                (
                    flag
                    for flag in flags
                    if flag.kind == "paste"
                    and flag.start is not None
                    and flag.start < verdict["end"]
                    and verdict["start"] < flag.end
                ),
                None,
            )
            if paste is not None:
                # Two independent signals on one passage: one stronger flag.
                paste.severity = "high"
                paste.other_user_id = verdict["style_user_id"]
                paste.title = "Pasted, and written in another collaborator's style"
                paste.detail += (
                    " A style model trained without this passage is also %d%% sure it "
                    "reads like a different collaborator's writing."
                    % round(verdict["style_confidence"] * 100)
                )
                continue
            flags.append(
                Flag(
                    kind="style_mismatch",
                    severity="high" if verdict["style_confidence"] >= 0.85 else "medium",
                    user_id=verdict["ledger_user_id"],
                    other_user_id=verdict["style_user_id"],
                    title="Typed by one person, written in another's style",
                    detail=(
                        "The keystroke ledger credits %d%% of this block to its typist, "
                        "but a style model trained without this block is %d%% sure it "
                        "reads like a different collaborator. Often a passage copied "
                        "from someone else's draft."
                        % (round(verdict["ledger_share"] * 100), round(verdict["style_confidence"] * 100))
                    ),
                    snippet=_snippet(verdict["content"]),
                    start=verdict["start"],
                    end=verdict["end"],
                )
            )

    people = {
        person.id: person
        for person in db.scalars(
            select(User).where(User.id.in_(set(stats) | set(surviving)))
        ).all()
    }
    contributors = []
    for user_id in set(stats) | set(surviving):
        person = people.get(user_id)
        entry = stats.get(user_id, {"operations": 0, "typed": 0, "pasted": 0, "deleted": 0})
        moments = sorted(times.get(user_id, []))
        sessions = 1 if moments else 0
        active = timedelta()
        for before, after in zip(moments, moments[1:]):
            gap = after - before
            if gap <= ACTIVE_GAP:
                active += gap
            if gap > SESSION_GAP:
                sessions += 1
        contributors.append(
            Contributor(
                user_id=user_id,
                name=person.name if person else "Former collaborator",
                color=person.presence_color if person else "#94a3b8",
                **entry,
                surviving=surviving.get(user_id, 0),
                share=round(surviving.get(user_id, 0) / len(content), 4) if content else 0.0,
                sessions=sessions,
                active_minutes=round(active.total_seconds() / 60, 1),
                first_edit=moments[0] if moments else None,
                last_edit=moments[-1] if moments else None,
            )
        )
    contributors.sort(key=lambda c: (-c.surviving, -c.typed, c.name))

    rank = {"high": 0, "medium": 1, "low": 2}
    flags.sort(key=lambda f: (rank[f.severity], f.start if f.start is not None else 1 << 30))
    inserted = sum(c.typed + c.pasted for c in contributors)
    return IntegrityReport(
        document_id=access.document.id,
        title=access.document.title,
        version=session.version,
        generated_at=datetime.now(timezone.utc),
        characters=len(content),
        keystroke_share=round(sum(c.typed for c in contributors) / inserted, 4) if inserted else 1.0,
        agreement_rate=round(agree / len(verdicts), 4) if verdicts else 0.0,
        judged_blocks=len(verdicts),
        model=session.engine.report.to_dict(),
        contributors=contributors,
        flags=flags,
    )


def _paste_flag(row, text: str, content: str) -> Flag:
    probe = text.strip()[:60]
    at = content.find(probe) if probe else -1
    present = at >= 0
    return Flag(
        kind="paste",
        # Pasted text that was later rewritten or removed matters less.
        severity="medium" if present else "low",
        user_id=row.user_id,
        title="Pasted in one go" if present else "Pasted, since removed or rewritten",
        detail=(
            "%d characters arrived in a single operation rather than as typing. "
            "The server cannot see where they came from%s."
            % (len(text), "" if present else "; the text is no longer on the page as pasted")
        ),
        snippet=_snippet(text),
        start=at if present else None,
        end=at + len(text) if present else None,
        version=row.version,
        timestamp=_aware(row.timestamp),
    )


def _leave_one_out(
    content: str, runs: list, samples: dict[str, list[str]], live: AttributionEngine
) -> list[dict]:
    """Judge each prose block with a model that never saw it.

    Returns one verdict per block the check could judge: who typed it (the
    ledger), and whose *other* writing it reads like (the style model).
    """
    trail = AuthorTrail.from_json(runs)
    candidates = []
    for parsed in block_parser.parse(content):
        if not parsed.prose or len(parsed.text.strip()) < MISMATCH_MIN_CHARS:
            continue
        typist, share = trail.dominant_of_range(parsed.start, parsed.end)
        if typist is not None:
            candidates.append((parsed, typist, share))
    candidates.sort(key=lambda c: -(c[0].end - c[0].start))

    verdicts = []
    for parsed, typist, share in candidates[:MAX_CHECKED_BLOCKS]:
        # The page and its ledger with this one block cut out.
        cut = TextOperation().retain(parsed.start).delete(parsed.end - parsed.start)
        cut.retain(len(content) - parsed.end)
        rest = AuthorTrail.from_json(runs)
        rest.apply(cut, typist)
        corpus = {user: list(chunks) for user, chunks in rest.text_by_author(cut.apply(content)).items()}
        for user, texts in samples.items():
            corpus.setdefault(user, []).extend(texts)

        engine = AttributionEngine(live.min_chars, live.min_samples_per_author)
        if not engine.train(corpus).trained or typist not in engine.report.authors:
            continue  # nothing of the typist's to compare against
        style_user, confidence = engine.attribute_text(parsed.text)
        verdicts.append(
            {
                "content": parsed.content,
                "start": parsed.start,
                "end": parsed.end,
                "ledger_user_id": typist,
                "ledger_share": share,
                "style_user_id": style_user,
                "style_confidence": confidence,
            }
        )
    verdicts.sort(key=lambda v: v["start"])
    return verdicts
