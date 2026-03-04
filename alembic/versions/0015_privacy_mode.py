"""Add privacy_mode column to users table.

Revision ID: 0015
Revises: 0014
Create Date: 2026-03-04
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add privacy_mode column — prevents model providers retaining training data."""
    op.add_column(
        "users",
        sa.Column(
            "privacy_mode",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="When enabled, prevents model providers from retaining training data",
        ),
    )
    op.create_index("ix_users_privacy_mode", "users", ["privacy_mode"])


def downgrade() -> None:
    op.drop_index("ix_users_privacy_mode", table_name="users")
    op.drop_column("users", "privacy_mode")
