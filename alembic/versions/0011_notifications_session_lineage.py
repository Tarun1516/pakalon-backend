"""Add notifications table and session code-change lineage columns.

Revision ID: 0011
Revises: 0010
Create Date: 2026-02-25 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    1. Create the 'notifications' table for in-app user notifications.
    2. Add lines_added / lines_deleted counters to 'sessions' for code change lineage.
    """
    # ── notifications table ────────────────────────────────────────────────
    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("notification_type", sa.String(100), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("body", sa.Text, nullable=False, server_default=""),
        sa.Column("action_url", sa.String(1024), nullable=True),
        sa.Column("action_label", sa.String(128), nullable=True),
        sa.Column("read", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_notifications_user_id_read",
        "notifications",
        ["user_id", "read"],
    )

    # ── sessions: lines_added / lines_deleted ──────────────────────────────
    op.add_column(
        "sessions",
        sa.Column("lines_added", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column(
        "sessions",
        sa.Column("lines_deleted", sa.Integer, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("sessions", "lines_deleted")
    op.drop_column("sessions", "lines_added")
    op.drop_index("ix_notifications_user_id_read", table_name="notifications")
    op.drop_table("notifications")
