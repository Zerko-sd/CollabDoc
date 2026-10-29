"""ORM models implementing the ER design of SRS section 6."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    """Dynamic permission access flags (SRS 6.3, 5.3 Authorization)."""

    OWNER = "OWNER"
    EDITOR = "EDITOR"
    VIEWER = "VIEWER"

    @property
    def rank(self) -> int:
        return {"VIEWER": 0, "EDITOR": 1, "OWNER": 2}[self.value]

    def can(self, required: "Role") -> bool:
        return self.rank >= required.rank


class BlockType(str, enum.Enum):
    """Node element type (SRS 6.5)."""

    HEADING = "HEADING"
    PARAGRAPH = "PARAGRAPH"
    LIST_ITEM = "LIST_ITEM"


class User(Base):
    """SRS 6.1 -- global user profiles, auth metadata, presence colour anchors."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    presence_color: Mapped[str] = mapped_column(String(7))
    password_hash: Mapped[str] = mapped_column(String(255))
    # Two-factor sign-in: a base32 TOTP secret, live once a code has confirmed it.
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    documents: Mapped[list["Document"]] = relationship(back_populates="owner")


class Document(Base):
    """SRS 6.2 -- structural master record for a file."""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(255), default="Untitled document")
    icon: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # OT revision: advances on every operation.
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    # What people see as "version N": advances once per editing burst.
    version_number: Mapped[int] = mapped_column(Integer, default=0)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    share_token: Mapped[str] = mapped_column(String(43), default=_uuid, index=True)
    share_role: Mapped[Role | None] = mapped_column(Enum(Role), nullable=True)
    # Pages nest: a sub-page points at the page it lives under.
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Set when the page is moved to the trash; cleared on restore.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A published page can be read by anyone with its share token, signed in or not.
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    # A locked page is read-only for everyone but its owner.
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    owner: Mapped[User] = relationship(back_populates="documents")
    collaborators: Mapped[list["DocumentCollaborator"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentCollaborator(Base):
    """SRS 6.3 -- RBAC mapping of users onto shared documents."""

    __tablename__ = "document_collaborators"
    __table_args__ = (UniqueConstraint("document_id", "user_id", name="uq_collaborator"),)

    # SQLite only auto-increments a plain INTEGER primary key, so the BigInt
    # of the SRS schema carries a variant for local runs.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.VIEWER)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    document: Mapped[Document] = relationship(back_populates="collaborators")
    user: Mapped[User] = relationship()


class DocumentOperation(Base):
    """SRS 6.4 -- the delta operational tracking engine.

    One row per accepted operational transformation, written in the order the
    server serialised it. Replaying rows 0..N reconstructs the document at
    version N, which is what the version-history endpoints do.
    """

    __tablename__ = "document_operations"
    __table_args__ = (
        Index("ix_operations_document_version", "document_id", "version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    delta_json: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentBlock(Base):
    """SRS 6.5 -- parsed layout matrix and the AI attribution log."""

    __tablename__ = "document_blocks"
    __table_args__ = (
        Index("ix_blocks_document_order", "document_id", "sort_order"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    block_type: Mapped[BlockType] = mapped_column(Enum(BlockType), default=BlockType.PARAGRAPH)
    content: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[float] = mapped_column(Float, default=0.0)
    attributed_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DocumentSnapshot(Base):
    """Operational snapshot block (SRS 6 hybrid model).

    Periodic materialisation of the document so a rollback to version N does
    not have to replay the entire operation log from zero.
    """

    __tablename__ = "document_snapshots"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_snapshot_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    trail_json: Mapped[str] = mapped_column(Text, default="[]")
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthorSample(Base):
    """Training corpus for the AI attribution engine (SRS 3.3, 6.2 WBS).

    Each row is a stretch of text the keystroke ledger proves a given user
    typed, which is what the stylometric classifier learns from.
    """

    __tablename__ = "author_samples"
    __table_args__ = (Index("ix_samples_document_user", "document_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentComment(Base):
    """A discussion thread pinned to a passage of a document.

    The anchor is the quoted text rather than a character offset: offsets move
    with every concurrent edit, while the quote survives them and can be found
    again in whatever the document has become.
    """

    __tablename__ = "document_comments"
    __table_args__ = (Index("ix_comments_document_created", "document_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_comments.id", ondelete="CASCADE"), nullable=True
    )
    anchor: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship()


class NotificationKind(str, enum.Enum):
    MENTION = "MENTION"
    COMMENT_REPLY = "COMMENT_REPLY"
    SHARED = "SHARED"


class Notification(Base):
    """Something another person did that the recipient should hear about."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[NotificationKind] = mapped_column(Enum(NotificationKind))
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    comment_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_comments.id", ondelete="CASCADE"), nullable=True
    )
    snippet: Mapped[str] = mapped_column(String(280), default="")
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    actor: Mapped[User] = relationship(foreign_keys=[actor_id])
    document: Mapped[Document] = relationship()


class Attachment(Base):
    """An uploaded file -- an image embedded in a page."""

    __tablename__ = "attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    uploader_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserFavorite(Base):
    """A page pinned to someone's sidebar, kept server-side so it follows them."""

    __tablename__ = "user_favorites"
    __table_args__ = (UniqueConstraint("user_id", "document_id", name="uq_favorite"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)


class AuditEvent(Base):
    """Who changed a page's sharing, place or lifecycle, and when.

    The operation log already says who typed what; this records the actions
    around the text -- invitations, role changes, publishing, the trash -- so
    an owner can answer "who gave that person access?".
    """

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_document_created", "document_id", "created_at"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    actor_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(String(280), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    actor: Mapped[User | None] = relationship()


class OAuthAccount(Base):
    """A sign-in identity at an external provider, linked to a local user."""

    __tablename__ = "oauth_accounts"
    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_oauth_identity"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    provider: Mapped[str] = mapped_column(String(20))
    subject: Mapped[str] = mapped_column(String(255))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CommentReaction(Base):
    """An emoji reaction on a comment: one per person, emoji and comment."""

    __tablename__ = "comment_reactions"
    __table_args__ = (UniqueConstraint("comment_id", "user_id", "emoji", name="uq_reaction"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    comment_id: Mapped[str] = mapped_column(
        ForeignKey("document_comments.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    emoji: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiToken(Base):
    """A personal access token for scripts and integrations.

    Only a SHA-256 of the token is stored: the token itself is shown once,
    when it is created, and a database leak does not hand out working keys.
    """

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
