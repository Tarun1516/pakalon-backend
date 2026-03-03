"""EmailQueue ORM model — pending outbound emails."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EmailQueue(Base):
    """An email pending delivery via Resend."""

    __tablename__ = "email_queue"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    to_email: Mapped[str] = mapped_column(String(512), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    html: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # trial_reminder_Xd | welcome | pro_renewal | etc.
    email_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # pending | sending | sent | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<EmailQueue id={self.id!r} type={self.email_type!r} status={self.status!r}>"
