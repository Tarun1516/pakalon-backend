"""AuditLog ORM model — governance audit trail (T-BACK-AUD)."""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, JSON, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AuditLog(Base):
    """Records every user-initiated or system action for compliance export."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    # Nullable: system-initiated actions have no user
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # e.g. "checkout.created", "subscription.canceled", "account.deleted"
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # e.g. "user", "subscription", "session", "model"
    resource_type: Mapped[str] = mapped_column(
        String(100), nullable=False, default="", server_default=""
    )
    # UUID or slug of the affected resource
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # IPv4 or IPv6 address (max 45 chars for IPv6)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    # Arbitrary structured context (request params, old/new values, etc.)
    extra: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="Arbitrary context for the audit event"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
        index=True,
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog id={self.id!r} action={self.action!r} "
            f"user={self.user_id!r} resource={self.resource_type}/{self.resource_id!r}>"
        )
