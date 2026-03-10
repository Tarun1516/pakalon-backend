"""LoginEvent ORM model — tracks authentication events with device and browser metadata."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


SQLITE_SAFE_INET = INET().with_variant(String(45), "sqlite")


class LoginEvent(Base):
    """Records each successful login with IP address and browser/device information."""

    __tablename__ = "login_events"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # "web" | "device_code" | "token"
    login_type: Mapped[str] = mapped_column(String(20), nullable=False, default="web")
    ip_address: Mapped[str | None] = mapped_column(SQLITE_SAFE_INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Parsed human-readable fields
    browser: Mapped[str | None] = mapped_column(String(100), nullable=True)
    os: Mapped[str | None] = mapped_column(String(100), nullable=True)
    device_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    machine_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<LoginEvent user_id={self.user_id!r} type={self.login_type!r}>"
