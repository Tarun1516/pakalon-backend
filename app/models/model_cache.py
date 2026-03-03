"""ModelCache ORM model — cached OpenRouter model metadata."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ModelCache(Base):
    """Cached metadata for AI models fetched from OpenRouter."""

    __tablename__ = "model_cache"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    model_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    context_length: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # free | paid
    tier: Mapped[str] = mapped_column(String(20), nullable=False, default="paid")
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
    )
    # OpenRouter `created` Unix-epoch → UTC — used for "newest models first" sort (migration 0009)
    model_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
        comment="Model release date from OpenRouter (Unix epoch converted to UTC)",
    )
    # Flag indicating if the cache entry is valid (True) or stale (False)
    cache_valid: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        index=True,
        comment="Whether the cache entry is valid (True) or stale (False)",
    )

    def __repr__(self) -> str:
        return f"<ModelCache model_id={self.model_id!r} tier={self.tier!r}>"
