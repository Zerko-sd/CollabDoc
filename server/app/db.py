"""Database engine and session wiring.

SRS 6 calls for a hybrid data model: a relational store for the transactional
entities and a high-throughput store for the delta stream. Both halves are
mapped here through SQLAlchemy so the deployment can point at SQLite (local
development) or PostgreSQL (production) with one environment variable.
"""
from collections.abc import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        # check_same_thread=False: the WebSocket hub touches the session from
        # the event loop's worker threads.
        # timeout: how long a connection waits for a lock before giving up.
        # Under sustained typing the saver writes constantly, and the default
        # is short enough that the attribution pass loses the race.
        return {"connect_args": {"check_same_thread": False, "timeout": 30}}
    return {"pool_pre_ping": True, "pool_size": 20, "max_overflow": 10}


_settings = get_settings()
engine = create_engine(_settings.database_url, future=True, **_engine_kwargs(_settings.database_url))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


if _settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        """Let readers and writers coexist.

        In SQLite's default rollback-journal mode a writer blocks every
        reader, so the once-a-second saver and the attribution pass contend
        for the whole database. Write-ahead logging lets them proceed
        concurrently, which is what keeps a burst of typing from starving the
        attribution worker.
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def init_db() -> None:
    from . import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=engine)
    added = _add_missing_columns()
    if ("documents", "version_number") in added:
        _backfill_version_numbers()


# Columns added after the first release. ``create_all`` creates missing tables
# but never alters existing ones, so a database from an older build gains them
# here instead of needing a migration tool for a handful of nullable fields.
_ADDED_COLUMNS = {
    "documents": {
        "icon": "VARCHAR(16)",
        "version_number": "INTEGER NOT NULL DEFAULT 0",
        # An upgraded database gets these without the foreign key: SQLite
        # cannot add a constraint to an existing table, and the trash code
        # cascades sub-pages itself rather than relying on it.
        "parent_id": "VARCHAR(36)",
        "deleted_at": "TIMESTAMP WITH TIME ZONE",
        "published": "BOOLEAN NOT NULL DEFAULT FALSE",
        "locked": "BOOLEAN NOT NULL DEFAULT FALSE",
    },
    "users": {
        "totp_secret": "VARCHAR(64)",
        "totp_enabled": "BOOLEAN NOT NULL DEFAULT FALSE",
    },
}


def _add_missing_columns() -> set[tuple[str, str]]:
    inspector = inspect(engine)
    added = set()
    for table, columns in _ADDED_COLUMNS.items():
        present = {column["name"] for column in inspector.get_columns(table)}
        for name, ddl in columns.items():
            if name not in present:
                with engine.begin() as connection:
                    connection.execute(text("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl)))
                added.add((table, name))
    return added


def _backfill_version_numbers() -> None:
    """Number the versions of documents written before versions were grouped."""
    from .models import Document
    from .realtime.versions import VersionCounter

    idle = get_settings().version_group_idle_seconds
    with SessionLocal() as db:
        for document in db.query(Document).all():
            document.version_number = VersionCounter.from_database(db, document.id, idle).number
        db.commit()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
