"""Add model_created_at to model_cache for OpenRouter release-date sorting.

Revision ID: 0009
Revises: 0008
Create Date: 2026-02-22 00:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    Add model_created_at column sourced from OpenRouter's `created` Unix epoch field.
    This enables sorting models by their actual release date (newest first) rather
    than by when Pakalon last fetched them.
    """
    op.add_column(
        "model_cache",
        sa.Column(
            "model_created_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Model release date from OpenRouter API (Unix epoch → UTC timestamp)",
        ),
    )
    # Index for fast ORDER BY model_created_at DESC queries
    op.create_index(
        "ix_model_cache_model_created_at",
        "model_cache",
        ["model_created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_model_cache_model_created_at", table_name="model_cache")
    op.drop_column("model_cache", "model_created_at")
