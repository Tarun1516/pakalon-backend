"""TelemetryEvent ORM model — analytics events."""
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TelemetryEvent(Base):
    """An analytics or telemetry event emitted by a CLI client.

    The table uses PostgreSQL range partitioning by created_at (monthly).
    The Alembic migration handles partition setup.
    """

    __tablename__ = "telemetry_events"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    properties: Mapped[Any | None] = mapped_column(JSONB, nullable=True, server_default=text("null"))
    cli_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    os_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
        index=True,
    )

    def __repr__(self) -> str:
        return f"<TelemetryEvent id={self.id!r} event={self.event_name!r}>"
