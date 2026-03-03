"""Message ORM model — a single turn within a session."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from typing import Any

from app.database import Base


class Message(Base):
    """A single chat message (user or assistant) within a session."""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    session_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # user | assistant | tool | system
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tool_calls: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<Message id={self.id!r} role={self.role!r} tokens={self.tokens_used}>"
