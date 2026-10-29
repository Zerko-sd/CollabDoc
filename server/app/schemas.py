"""Request and response contracts (WBS 2.3 API contracts)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from .models import BlockType, NotificationKind, Role


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# -- auth -----------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(ORMModel):
    id: str
    email: str
    name: str
    avatar_url: str | None = None
    presence_color: str


class MeOut(UserOut):
    """The signed-in user's own profile, with their security settings."""

    totp_enabled: bool = False


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class MfaChallenge(BaseModel):
    """The password was right; a one-time code is still needed."""

    mfa_required: bool = True
    mfa_token: str


class MfaVerify(BaseModel):
    mfa_token: str
    code: str = Field(min_length=6, max_length=8)


class TotpCode(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class TotpSetup(BaseModel):
    secret: str
    uri: str


class ApiTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ApiTokenOut(BaseModel):
    id: str
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None = None


class ApiTokenCreated(ApiTokenOut):
    token: str  # shown once


# -- documents ------------------------------------------------------------


class DocumentCreate(BaseModel):
    title: str = Field(default="Untitled document", max_length=255)
    content: str = ""
    icon: str | None = Field(default=None, max_length=16)
    parent_id: str | None = None


class DocumentUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    # An empty string clears the icon; omitting the field leaves it alone.
    icon: str | None = Field(default=None, max_length=16)
    # Moves the page. Sending null moves it to the top level; omitting the
    # field leaves it where it is (told apart through ``model_fields_set``).
    parent_id: str | None = None


class DocumentSummary(ORMModel):
    id: str
    title: str
    current_version: int
    version_number: int = 0
    owner_id: str
    created_at: datetime
    updated_at: datetime
    role: Role
    collaborator_count: int = 0
    icon: str | None = None
    parent_id: str | None = None
    deleted_at: datetime | None = None
    published: bool = False
    locked: bool = False


class DocumentDetail(DocumentSummary):
    content: str
    share_token: str | None = None
    share_role: Role | None = None


class DocumentSaveRequest(BaseModel):
    """Manual save (SRS 3.3): the client sends the buffer, the server diffs it."""

    content: str
    label: str | None = Field(default=None, max_length=120)


# -- collaborators --------------------------------------------------------


class CollaboratorAdd(BaseModel):
    email: EmailStr
    role: Role = Role.EDITOR


class CollaboratorRoleUpdate(BaseModel):
    role: Role


class CollaboratorOut(BaseModel):
    user: UserOut
    role: Role
    # True when access comes from a parent page rather than a grant here.
    inherited: bool = False


class ShareLinkRequest(BaseModel):
    role: Role | None = Field(
        default=Role.VIEWER,
        description="Role granted by the link; null disables link sharing.",
    )


class ShareLinkOut(BaseModel):
    share_token: str | None
    share_role: Role | None
    url: str | None


# -- versions -------------------------------------------------------------


class VersionEntry(BaseModel):
    # The burst covers operations first_version..version; restoring ``version``
    # gives the document as it stood when the author paused.
    version: int
    number: int
    first_version: int
    operation_count: int
    user_id: str
    user_name: str | None = None
    delta: list[Any]
    started_at: datetime
    timestamp: datetime
    label: str | None = None


class VersionContent(BaseModel):
    version: int
    content: str


class RevertRequest(BaseModel):
    version: int = Field(ge=0)


# -- attribution ----------------------------------------------------------


class BlockOut(BaseModel):
    index: int
    block_type: BlockType
    content: str
    start: int
    end: int
    sort_order: float
    attributed_user_id: str | None
    confidence_score: float
    method: str
    evidence: dict[str, Any] = {}


class AttributionOut(BaseModel):
    document_id: str
    version: int
    blocks: list[BlockOut]
    model: dict[str, Any]


class AttributionMetrics(BaseModel):
    document_id: str
    version: int
    model: dict[str, Any]
    block_count: int
    mean_confidence: float
    agreement_rate: float
    per_author: dict[str, dict[str, float]]


# -- comments -------------------------------------------------------------


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    anchor: str = Field(default="", max_length=2000)
    parent_id: str | None = None


class CommentUpdate(BaseModel):
    resolved: bool | None = None
    body: str | None = Field(default=None, min_length=1, max_length=4000)


class CommentOut(BaseModel):
    id: str
    document_id: str
    parent_id: str | None
    anchor: str
    body: str
    resolved: bool
    created_at: datetime
    user: UserOut
    # emoji -> ids of the people who reacted with it
    reactions: dict[str, list[str]] = Field(default_factory=dict)


# -- search and activity --------------------------------------------------


class SearchHit(BaseModel):
    document_id: str
    title: str
    icon: str | None = None
    role: Role
    snippet: str | None = None
    updated_at: datetime


class ActivityAuthor(BaseModel):
    user_id: str
    name: str
    presence_color: str
    operations: int  # raw OT operations -- roughly keystrokes
    inserted: int
    deleted: int
    sessions: int
    active_seconds: int


class ActivityBucket(BaseModel):
    start: datetime
    counts: dict[str, int]  # characters changed (inserted + deleted) per author


class ActivitySession(BaseModel):
    """One sitting of editing: the same grouping the version history uses."""

    user_id: str
    name: str
    presence_color: str
    started_at: datetime
    ended_at: datetime
    inserted: int
    deleted: int


class ActivityOut(BaseModel):
    document_id: str
    bucket_seconds: int
    buckets: list[ActivityBucket]
    authors: list[ActivityAuthor]
    sessions: list[ActivitySession]  # newest first
    first_edit: datetime | None
    last_edit: datetime | None


# -- notifications --------------------------------------------------------


class NotificationOut(BaseModel):
    id: str
    kind: NotificationKind
    document_id: str
    document_title: str
    document_icon: str | None = None
    comment_id: str | None = None
    snippet: str
    actor: UserOut
    read: bool
    created_at: datetime


class NotificationsRead(BaseModel):
    # Omit (or send null) to mark everything read.
    ids: list[str] | None = None


class MentionCreate(BaseModel):
    user_id: str
    snippet: str = Field(default="", max_length=280)


# -- attachments ----------------------------------------------------------


class AttachmentOut(BaseModel):
    id: str
    url: str
    filename: str
    mime: str
    size: int


# -- favorites and publishing ---------------------------------------------


class FavoritesUpdate(BaseModel):
    document_ids: list[str] = Field(max_length=200)


class PublishRequest(BaseModel):
    published: bool


class PublishOut(BaseModel):
    published: bool
    url: str | None


# -- audit log ------------------------------------------------------------


class AuditEntry(BaseModel):
    action: str
    detail: str
    actor: UserOut | None
    created_at: datetime


# -- writing assistant ----------------------------------------------------


class AssistRequest(BaseModel):
    action: str = Field(pattern="^(summarize|improve|continue|outline)$")
    # The selection to work on; empty means the whole page.
    text: str = Field(default="", max_length=60_000)


class AssistOut(BaseModel):
    text: str
    source: str
    model: str | None = None
