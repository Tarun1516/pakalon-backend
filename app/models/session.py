"""Session ORM model — represents a chat session."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Session(Base):
    """A Pakalon chat session tied to a user, machine, and project directory."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str | None] = mapped_column(String(512), nullable=True, default="New Chat")
    mode: Mapped[str | None] = mapped_column(String(32), nullable=True, default="chat")
    machine_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    project_dir: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Epic A-05: Track what % of the context window was consumed this session
    context_pct_used: Mapped[float | None] = mapped_column(
        Numeric(5, 2), nullable=True, default=None
    )
    # Code change lineage — lines added and deleted across all edits in this session
    lines_added: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lines_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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

    def __repr__(self) -> str:
        return f"<Session id={self.id!r} user_id={self.user_id!r}>"
