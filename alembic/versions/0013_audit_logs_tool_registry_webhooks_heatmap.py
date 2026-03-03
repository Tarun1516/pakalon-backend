"""Add audit_logs, tool_registry, webhook_dead_letters, contribution_days tables.

Revision ID: 0013
Revises: 0012
Create Date: 2026-03-05 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create audit_logs, tool_registry, webhook_dead_letters, contribution_days tables."""

    # ── audit_logs ─────────────────────────────────────────────────────────────
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("user_id", sa.String(255), nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_type", sa.String(100), nullable=False, server_default=""),
        sa.Column("resource_id", sa.String(255), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])

    # ── tool_registry ──────────────────────────────────────────────────────────
    op.create_table(
        "tool_registry",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "category",
            postgresql.ENUM("sast", "dast", "sca", "other", name="toolcategory"),
            nullable=False,
        ),
        sa.Column("install_command", sa.Text(), nullable=True),
        sa.Column("run_command", sa.String(500), nullable=True),
        sa.Column("requires_docker", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_pro", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_tool_registry_name", "tool_registry", ["name"])

    # ── webhook_dead_letters ───────────────────────────────────────────────────
    op.create_table(
        "webhook_dead_letters",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("service", sa.String(50), nullable=False),
        sa.Column("operation", sa.String(100), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_attempted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_webhook_dead_letters_service", "webhook_dead_letters", ["service"])
    op.create_index("ix_webhook_dead_letters_created_at", "webhook_dead_letters", ["created_at"])

    # ── contribution_days ──────────────────────────────────────────────────────
    op.create_table(
        "contribution_days",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("contribution_date", sa.Date(), nullable=False),
        sa.Column("lines_added", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lines_deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("commits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sessions_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_contribution_days_user_id", "contribution_days", ["user_id"])
    op.create_index("ix_contribution_days_date", "contribution_days", ["contribution_date"])
    # Unique constraint: one row per user per day
    op.create_unique_constraint(
        "uq_contribution_days_user_date",
        "contribution_days",
        ["user_id", "contribution_date"],
    )


def downgrade() -> None:
    """Drop the tables added by this migration."""
    op.drop_table("contribution_days")
    op.drop_table("webhook_dead_letters")
    op.drop_table("tool_registry")
    op.drop_table("audit_logs")

    # Drop custom enum types
    op.execute("DROP TYPE IF EXISTS toolstatus")
    op.execute("DROP TYPE IF EXISTS toolcategory")
