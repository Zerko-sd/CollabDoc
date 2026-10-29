"""Features that look across pages, or at a page's whole life.

* backlinks and the graph: which pages link to which;
* the raw operation log, for the history time-lapse;
* duplicating a page (with its sub-pages) and locking one;
* "my tasks" -- to-dos assigned to the caller, wherever they are -- and a
  week of the caller's own writing, for the home dashboard.

Page text is not a column: it is the operation log replayed. Each request
here reads it through ``live_session``, which uses the open room when there
is one and otherwise replays from the latest snapshot.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..db import get_db
from ..deps import (
    DocumentAccess,
    accessible_documents,
    document_access,
    get_current_user,
    require_owner,
    subtree_ids,
)
from ..models import Document, DocumentOperation, Role, User
from ..notifications import MENTION, plain
from ..realtime.hub import RoomRegistry
from ..realtime.ot import TextOperation
from ..realtime.session import DocumentSession
from .documents import _summary, get_registry, live_session
from ..schemas import DocumentSummary

router = APIRouter(prefix="/api", tags=["pages"])

# Cross-page scans read every page the caller can open; past this many they
# stop, rather than replay an unbounded number of logs in one request.
SCAN_LIMIT = 300
PAGE_LINK = re.compile(r"\[([^\]\n]*)\]\(page:([0-9a-f-]{36})\)")
TODO = re.compile(r"^\s*[-*+] \[( |x|X)\] (.*)$")
DUE = re.compile(r"📅\s?(\d{4}-\d{2}-\d{2})")


async def _contents(registry: RoomRegistry, ids: list[str]) -> dict[str, str]:
    contents = {}
    for document_id in ids[:SCAN_LIMIT]:
        session = await live_session(registry, document_id)
        contents[document_id] = session.content
    return contents


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# -- backlinks and the graph --------------------------------------------------


class Backlink(BaseModel):
    document_id: str
    title: str
    icon: str | None = None
    snippet: str


@router.get("/documents/{document_id}/backlinks", response_model=list[Backlink])
async def backlinks(
    access: DocumentAccess = Depends(document_access),
    registry: RoomRegistry = Depends(get_registry),
    db: Session = Depends(get_db),
) -> list[Backlink]:
    """Pages the caller can open that link to this one."""
    found = accessible_documents(db, access.user)
    others = [doc_id for doc_id in found if doc_id != access.document.id]
    needle = "](page:%s)" % access.document.id
    links = []
    for doc_id, content in (await _contents(registry, others)).items():
        if needle not in content:
            continue
        line = next(line for line in content.split("\n") if needle in line)
        document = found[doc_id][0]
        links.append(
            Backlink(
                document_id=doc_id,
                title=document.title,
                icon=document.icon,
                snippet=plain(PAGE_LINK.sub(lambda m: m.group(1), line), 160),
            )
        )
    links.sort(key=lambda link: link.title.lower())
    return links


class GraphNode(BaseModel):
    id: str
    title: str
    icon: str | None = None
    words: int


class GraphEdge(BaseModel):
    source: str
    target: str
    kind: str  # "parent" or "link"


class GraphOut(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


@router.get("/graph", response_model=GraphOut)
async def graph(
    registry: RoomRegistry = Depends(get_registry),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> GraphOut:
    """Every page the caller can open, with nesting and page links as edges."""
    found = accessible_documents(db, user)
    contents = await _contents(registry, list(found))
    nodes, edges, seen = [], [], set()
    for doc_id, content in contents.items():
        document = found[doc_id][0]
        words = len(re.findall(r"[\w']+", PAGE_LINK.sub(lambda m: m.group(1), content)))
        nodes.append(GraphNode(id=doc_id, title=document.title, icon=document.icon, words=words))
        if document.parent_id in contents:
            edges.append(GraphEdge(source=document.parent_id, target=doc_id, kind="parent"))
        for match in PAGE_LINK.finditer(content):
            target = match.group(2)
            key = (doc_id, target)
            if target in contents and target != doc_id and key not in seen:
                seen.add(key)
                edges.append(GraphEdge(source=doc_id, target=target, kind="link"))
    return GraphOut(nodes=nodes, edges=edges)


# -- the operation log, for the time-lapse --------------------------------------


class OperationOut(BaseModel):
    version: int
    user_id: str
    delta: list
    timestamp: datetime


class OperationPage(BaseModel):
    operations: list[OperationOut]
    names: dict[str, str]
    colors: dict[str, str]
    next_after: int | None  # pass as ``after`` for the next page; None at the end


@router.get("/documents/{document_id}/operations", response_model=OperationPage)
async def operations(
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=2000, ge=1, le=5000),
    access: DocumentAccess = Depends(document_access),
    registry: RoomRegistry = Depends(get_registry),
    db: Session = Depends(get_db),
) -> OperationPage:
    """Operations after revision ``after``, oldest first.

    Replaying them from an empty page reproduces the document keystroke by
    keystroke -- which is what the history time-lapse draws.
    """
    session = await live_session(registry, access.document.id)
    await session.flush()
    rows = db.execute(
        select(
            DocumentOperation.version,
            DocumentOperation.user_id,
            DocumentOperation.delta_json,
            DocumentOperation.timestamp,
        )
        .where(DocumentOperation.document_id == access.document.id, DocumentOperation.version > after)
        .order_by(DocumentOperation.version)
        .limit(limit + 1)
    ).all()
    more = len(rows) > limit
    rows = rows[:limit]
    people = {
        person.id: person
        for person in db.scalars(select(User).where(User.id.in_({row.user_id for row in rows}))).all()
    }
    return OperationPage(
        operations=[
            OperationOut(version=row.version, user_id=row.user_id, delta=json.loads(row.delta_json), timestamp=row.timestamp)
            for row in rows
        ],
        names={uid: person.name for uid, person in people.items()},
        colors={uid: person.presence_color for uid, person in people.items()},
        next_after=rows[-1].version if more and rows else None,
    )


# -- duplicate and lock ------------------------------------------------------------


class DuplicateRequest(BaseModel):
    include_subpages: bool = False


@router.post(
    "/documents/{document_id}/duplicate",
    response_model=DocumentSummary,
    status_code=status.HTTP_201_CREATED,
)
async def duplicate(
    payload: DuplicateRequest,
    access: DocumentAccess = Depends(document_access),
    registry: RoomRegistry = Depends(get_registry),
    db: Session = Depends(get_db),
) -> DocumentSummary:
    """Copy a page (and optionally the sub-pages the caller can see) into new
    pages the caller owns. Links between copied pages point at the copies."""
    found = accessible_documents(db, access.user)
    ids = subtree_ids(db, access.document.id) if payload.include_subpages else [access.document.id]
    ids = [doc_id for doc_id in ids if doc_id in found][:SCAN_LIMIT]
    contents = await _contents(registry, ids)

    copies: dict[str, Document] = {}
    for doc_id in ids:
        source = found[doc_id][0]
        is_root = doc_id == access.document.id
        copy = Document(
            title=(source.title + " (copy)") if is_root else source.title,
            icon=source.icon,
            owner_id=access.user.id,
            # The root copy sits beside the original if the caller may add
            # pages there; otherwise at the top level.
            parent_id=(
                (source.parent_id if source.parent_id in found and found[source.parent_id][1].can(Role.EDITOR) else None)
                if is_root
                else copies[source.parent_id].id
            ),
        )
        db.add(copy)
        db.flush()
        copies[doc_id] = copy
    audit.record(db, copies[access.document.id].id, access.user, "created", "as a copy of %s" % access.document.title)
    db.commit()

    for doc_id, copy in copies.items():
        content = contents.get(doc_id, "")
        for old, new in copies.items():
            content = content.replace("](page:%s)" % old, "](page:%s)" % new.id)
        if content:
            session = DocumentSession(copy.id, get_settings())
            await session.load()
            await session.submit(access.user.id, 0, TextOperation().insert(content))
            await session.flush()
    root = copies[access.document.id]
    db.refresh(root)
    return _summary(db, root, Role.OWNER)


class LockRequest(BaseModel):
    locked: bool


@router.put("/documents/{document_id}/lock", response_model=DocumentSummary)
async def lock(
    payload: LockRequest,
    access: DocumentAccess = Depends(require_owner),
    registry: RoomRegistry = Depends(get_registry),
    db: Session = Depends(get_db),
) -> DocumentSummary:
    """Make a page read-only for everyone but its owner, or undo that."""
    if access.document.locked != payload.locked:
        access.document.locked = payload.locked
        audit.record(db, access.document.id, access.user, "locked" if payload.locked else "unlocked", "")
        db.commit()
        # Everyone reconnects and is told their new role by the room.
        await registry.force_close(access.document.id)
    return _summary(db, access.document, access.role)


# -- my tasks ------------------------------------------------------------------------


class TaskOut(BaseModel):
    document_id: str
    title: str
    icon: str | None = None
    line: int
    text: str
    due: date | None = None
    assigned: bool  # the caller is @mentioned on the to-do


@router.get("/me/tasks", response_model=list[TaskOut])
async def my_tasks(
    registry: RoomRegistry = Depends(get_registry),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[TaskOut]:
    """Open to-dos assigned to the caller, plus unassigned ones on their own pages.

    Soonest due first; undated last.
    """
    found = accessible_documents(db, user)
    tasks = []
    for doc_id, content in (await _contents(registry, list(found))).items():
        document, role = found[doc_id]
        for index, line in enumerate(content.split("\n")):
            match = TODO.match(line)
            if not match or match.group(1) != " ":
                continue
            body = match.group(2)
            people = [m.group(2) for m in MENTION.finditer(body)]
            assigned = user.id in people
            if not assigned and (people or role is not Role.OWNER):
                continue
            due = DUE.search(body)
            try:
                due_date = date.fromisoformat(due.group(1)) if due else None
            except ValueError:
                due_date = None
            text = plain(DUE.sub("", PAGE_LINK.sub(lambda m: m.group(1), body)), 200).strip()
            tasks.append(
                TaskOut(
                    document_id=doc_id, title=document.title, icon=document.icon,
                    line=index, text=text or "Untitled task", due=due_date, assigned=assigned,
                )
            )
    tasks.sort(key=lambda task: (task.due is None, task.due or date.max, task.title.lower(), task.line))
    return tasks


# -- a week of writing ---------------------------------------------------------------


class DayCount(BaseModel):
    day: date
    words: int


class StatsOut(BaseModel):
    words_this_week: int
    days: list[DayCount] = Field(description="The last seven days, oldest first")
    pages: int
    streak: int  # consecutive days, ending today or yesterday, with writing


@router.get("/me/stats", response_model=StatsOut)
def my_stats(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> StatsOut:
    """How much the caller wrote, from their own operations in the log.

    Words are estimated as inserted characters / 5 -- the usual typing-test
    convention -- which counts pasted and typed text alike.
    """
    today = datetime.now(timezone.utc).date()
    since = datetime.combine(today - timedelta(days=29), datetime.min.time(), tzinfo=timezone.utc)
    chars: dict[date, int] = {}
    for delta_json, timestamp in db.execute(
        select(DocumentOperation.delta_json, DocumentOperation.timestamp).where(
            DocumentOperation.user_id == user.id, DocumentOperation.timestamp >= since
        )
    ):
        day = _aware(timestamp).date()
        chars[day] = chars.get(day, 0) + sum(len(c) for c in json.loads(delta_json) if isinstance(c, str))
    days = [
        DayCount(day=today - timedelta(days=offset), words=round(chars.get(today - timedelta(days=offset), 0) / 5))
        for offset in range(6, -1, -1)
    ]
    streak = 0
    cursor = today if chars.get(today) else today - timedelta(days=1)
    while chars.get(cursor):
        streak += 1
        cursor -= timedelta(days=1)
    return StatsOut(
        words_this_week=sum(day.words for day in days),
        days=days,
        pages=len(accessible_documents(db, user)),
        streak=streak,
    )
