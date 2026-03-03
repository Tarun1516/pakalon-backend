"""CreditLedger ORM model — tracks per-user credit allocations and usage."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CreditLedger(Base):
    """
    Stores the credit balance for a single billing period.

    One row per (user_id, period_start) pair.  The service layer creates a
    new row each billing cycle and debits `credits_used` on every AI call.

    Credit limits by plan:
      free      →  credits_total = 0 (no credits, token-based free tier only)
      trial     →  credits_total = 50
      pro       →  credits_total = 500
      enterprise →  credits_total = 5000
    """

    __tablename__ = "credit_ledger"
    __table_args__ = (
        UniqueConstraint("user_id", "period_start", name="uq_credit_ledger_user_period"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan: Mapped[str] = mapped_column(String(20), nullable=False)
    credits_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    credits_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
        onupdate=lambda: datetime.now(tz=timezone.utc),
    )

    @property
    def credits_remaining(self) -> int:
        return max(0, self.credits_total - self.credits_used)

    def __repr__(self) -> str:
        return (
            f"<CreditLedger user={self.user_id!r} plan={self.plan!r} "
            f"used={self.credits_used}/{self.credits_total}>"
        )
