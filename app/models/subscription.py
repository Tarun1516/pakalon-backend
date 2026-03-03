"""Subscription ORM model."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Subscription(Base):
    """Tracks a Polar subscription for a user."""

    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    polar_sub_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    polar_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # active | canceled | past_due | paused | unpaid
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grace_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    amount_usd: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True, default=22.00)
    payment_method: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<Subscription id={self.id!r} user_id={self.user_id!r} status={self.status!r}>"
