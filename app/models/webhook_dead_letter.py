"""WebhookDeadLetter ORM model — dead-letter queue for failed outbound calls."""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WebhookDeadLetter(Base):
    """
    Persists outbound webhook / API calls that permanently failed after all
    retry attempts (Polar checkouts, Polar subscription cancels, Resend email).

    Allows ops to inspect + manually replay failed operations without data loss.
    """

    __tablename__ = "webhook_dead_letters"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    # e.g. "polar" | "resend"
    service: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # e.g. "checkouts.create" | "subscriptions.cancel" | "send_email"
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    # The original request payload (serializable dict)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # Last exception message
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Total attempts made before giving up
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Whether a human has manually reviewed/resolved this record
    resolved: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default="false",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
        index=True,
    )
    last_attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
        onupdate=lambda: datetime.now(tz=timezone.utc),
    )

    def __repr__(self) -> str:
        return (
            f"<WebhookDeadLetter id={self.id!r} service={self.service!r} "
            f"op={self.operation!r} attempts={self.attempts}>"
        )
