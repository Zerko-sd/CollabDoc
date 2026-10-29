"""Application settings (SRS 2.4 Operating Environment)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="COLLABDOC_", env_file=".env", extra="ignore"
    )

    secret_key: str = "dev-only-insecure-secret-change-me"
    database_url: str = "sqlite:///./collabdoc.db"
    access_token_minutes: int = 720
    algorithm: str = "HS256"

    # Real-time fan-out backend. "memory" keeps the pub/sub in-process; the
    # Broadcaster interface is the seam where Kafka slots in for the
    # multi-node deployment drawn in SRS 2.4.
    broadcaster: str = "memory"

    # AI attribution engine (SRS 3.3, 6.Document_Blocks)
    attribution_min_chars: int = 120
    attribution_min_samples_per_author: int = 3

    # Document saving (SRS 3.3 "Automatic and manual saving options")
    autosave_debounce_ms: int = 800

    # Snapshot cadence for fast version rollback.
    snapshot_every_n_ops: int = 25

    # Version history lists editing bursts, not raw operations: consecutive
    # operations by one author with no pause longer than this collapse into a
    # single entry.
    version_group_idle_seconds: float = 60.0

    # Uploaded images live on disk here, one file per attachment -- or, when
    # a bucket is named, in S3, so every replica serves the same images.
    upload_dir: str = "./uploads"
    upload_bucket: str = ""
    upload_max_bytes: int = 10 * 1024 * 1024

    # Requests per minute per client address; 0 turns a limit off.
    auth_rate_limit: int = 20
    # Requests per minute per user to the AI assistant.
    assist_rate_limit: int = 12

    # The writing assistant: "auto" uses Claude when credentials are
    # configured and the local summariser otherwise; "claude" or "local"
    # forces one.
    ai_provider: str = "auto"
    ai_model: str = "claude-opus-5-5"

    # Sign-in with GitHub or Google. A provider is offered once both its id
    # and secret are set. ``public_url`` is where the provider sends people
    # back to; it defaults to the address the request arrived on.
    public_url: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""

    # Kafka fan-out (COLLABDOC_BROADCASTER=kafka:host:9092).
    kafka_topic: str = "collabdoc.events"


@lru_cache
def get_settings() -> Settings:
    return Settings()
