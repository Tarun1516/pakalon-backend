"""Add figma_pat column to users for Figma Design API access.

Revision ID: 0010
Revises: 0009
Create Date: 2026-02-22 01:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add figma_pat column for storing encrypted Figma Personal Access Tokens."""
    op.add_column(
        "users",
        sa.Column(
            "figma_pat",
            sa.String(512),
            nullable=True,
            comment="Figma Personal Access Token (stored encrypted). Used to call Figma REST API.",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "figma_pat")
