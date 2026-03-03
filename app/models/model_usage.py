"""ModelUsage ORM model — tracks token usage per AI call (T-BACK-01)."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ModelUsage(Base):
    """Records token consumption for every AI inference call."""

    __tablename__ = "model_usage"

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
    session_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    model_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )
    # Total tokens consumed (prompt + completion)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Context window size reported by the model
    context_window_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # Tokens already filling the context at the time of this call
    context_window_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # Lines of code written (estimated from completion delta)
    lines_written: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
        index=True,
    )

    def __repr__(self) -> str:
        return (
            f"<ModelUsage id={self.id!r} user_id={self.user_id!r}"
            f" model_id={self.model_id!r} tokens={self.tokens_used!r}>"
        )
