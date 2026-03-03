"""Create sessions and messages tables.

Revision ID: 0004
Revises: 0003
Create Date: 2026-02-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── sessions ──────────────────────────────────────────────
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("machine_id", sa.String(512), nullable=True),
        sa.Column("project_dir", sa.String(255), nullable=True),
        sa.Column("model_id", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_project_dir", "sessions", ["project_dir"])

    # ── messages ──────────────────────────────────────────────
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_messages_session_id", "messages", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_messages_session_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_sessions_project_dir", table_name="sessions")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
