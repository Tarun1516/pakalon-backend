"""Add context_pct_used to sessions table (Epic A-05).

Revision ID: 0008
Revises: 0007
Create Date: 2026-02-22 00:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "context_pct_used",
            sa.Numeric(5, 2),
            nullable=True,
            comment="Percentage of context window consumed in this session (0.00-100.00)",
        ),
    )


def downgrade() -> None:
    op.drop_column("sessions", "context_pct_used")
