"""Create model_cache and email_queue tables.

Revision ID: 0006
Revises: 0005
Create Date: 2026-02-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── model_cache ───────────────────────────────────────────
    op.create_table(
        "model_cache",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(500), nullable=False, server_default=""),
        sa.Column("provider", sa.String(255), nullable=False, server_default=""),
        sa.Column("context_window", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pricing_tier", sa.String(20), nullable=False, server_default="pro"),
        sa.Column("supports_tools", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "raw_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_id", name="uq_model_cache_model_id"),
    )
    op.create_index("ix_model_cache_model_id", "model_cache", ["model_id"])

    # ── email_queue ───────────────────────────────────────────
    op.create_table(
        "email_queue",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("email_type", sa.String(100), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_email_queue_user_id", "email_queue", ["user_id"])
    op.create_index("ix_email_queue_scheduled_for", "email_queue", ["scheduled_for"])
    op.create_index("ix_email_queue_status", "email_queue", ["status"])


def downgrade() -> None:
    op.drop_index("ix_email_queue_status", table_name="email_queue")
    op.drop_index("ix_email_queue_scheduled_for", table_name="email_queue")
    op.drop_index("ix_email_queue_user_id", table_name="email_queue")
    op.drop_table("email_queue")
    op.drop_index("ix_model_cache_model_id", table_name="model_cache")
    op.drop_table("model_cache")
