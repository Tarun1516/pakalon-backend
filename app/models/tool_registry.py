"""ToolRegistry ORM model — tracks available security tools and their installation status."""
import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import DateTime, Enum as SQLEnum, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ToolCategory(str, Enum):
    """Categories of security tools."""
    SAST = "sast"  # Static Application Security Testing
    DAST = "dast"  # Dynamic Application Security Testing
    SCA = "sca"    # Software Composition Analysis
    OTHER = "other"


class ToolStatus(str, Enum):
    """Installation status of a tool."""
    NOT_INSTALLED = "not_installed"
    INSTALLING = "installing"
    INSTALLED = "installed"
    FAILED = "failed"


class ToolRegistry(Base):
    """Registry of available security tools and their status."""

    __tablename__ = "tool_registry"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        index=True,
    )
    display_name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    category: Mapped[ToolCategory] = mapped_column(
        SQLEnum(ToolCategory),
        nullable=False,
    )
    install_command: Mapped[str] = mapped_column(
        Text,
        nullable=True,
    )
    run_command: Mapped[str] = mapped_column(
        String(500),
        nullable=True,
    )
    requires_docker: Mapped[bool] = mapped_column(
        Integer,
        nullable=False,
        default=False,
    )
    is_pro: Mapped[bool] = mapped_column(
        Integer,
        nullable=False,
        default=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(tz=timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<ToolRegistry name={self.name!r} category={self.category!r}>"
