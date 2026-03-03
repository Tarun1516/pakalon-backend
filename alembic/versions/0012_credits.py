"""Add credit_ledger table for global credits system.

Revision ID: 0012
Revises: 0011
Create Date: 2026-03-04 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the credit_ledger table for per-user per-period credit tracking."""
    op.create_table(
        "credit_ledger",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("plan", sa.String(20), nullable=False),
        sa.Column("credits_total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("credits_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "period_start",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "period_end",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", "period_start", name="uq_credit_ledger_user_period"),
    )
    op.create_index("ix_credit_ledger_user_id", "credit_ledger", ["user_id"])
    op.create_index(
        "ix_credit_ledger_user_period",
        "credit_ledger",
        ["user_id", "period_start"],
    )


def downgrade() -> None:
    """Drop the credit_ledger table."""
    op.drop_index("ix_credit_ledger_user_period", table_name="credit_ledger")
    op.drop_index("ix_credit_ledger_user_id", table_name="credit_ledger")
    op.drop_table("credit_ledger")
