"""Notification ORM model — in-app notifications for users (T-BACK-NOTIFY)."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Notification(Base):
    """
    An in-app notification surfaced directly inside the Pakalon dashboard/CLI.

    Types:
      billing_reminder   — trial or subscription nearing expiry
      trial_expiry       — free trial has expired, prompt to upgrade
      context_exhausted  — context window 0% remaining for a model
      plan_upgrade       — upsell nudge from free → pro
      grace_period       — grace period warning before downgrade
    """

    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Notification category — drives icon and CTA in the UI
    notification_type: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Optional deep-link action URL shown alongside the notification
    action_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    action_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Read state — updated via PATCH /notifications/{id}/read
    read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
    )
    # Optional TTL — stale notifications may be hidden after this date
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<Notification id={self.id!r} user_id={self.user_id!r} "
            f"type={self.notification_type!r} read={self.read!r}>"
        )
