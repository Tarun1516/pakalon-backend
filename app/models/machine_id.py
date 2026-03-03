"""MachineId ORM model."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MachineId(Base):
    """Tracks machine fingerprints to detect trial abuse and support machine-level access."""

    __tablename__ = "machine_ids"
    __table_args__ = (
        UniqueConstraint("user_id", "machine_id", name="uq_machine_ids_user_machine"),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    machine_id: Mapped[str] = mapped_column(String(512), nullable=False)
    mac_machine_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    dev_device_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    os_info: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
        onupdate=lambda: datetime.now(tz=timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<MachineId user_id={self.user_id!r} machine_id={self.machine_id[:16]!r}>"
