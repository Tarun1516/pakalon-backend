"""model_usage table (T-BACK-01).

Revision ID: 0007
Revises: 0006_model_cache_email_queue
Create Date: 2025-01-01 00:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_usage",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("tokens_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "context_window_size", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column(
            "context_window_used", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column(
            "lines_written", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_model_usage_user_id", "model_usage", ["user_id"])
    op.create_index("ix_model_usage_session_id", "model_usage", ["session_id"])
    op.create_index("ix_model_usage_model_id", "model_usage", ["model_id"])
    op.create_index("ix_model_usage_created_at", "model_usage", ["created_at"])


def downgrade() -> None:
    op.drop_table("model_usage")
