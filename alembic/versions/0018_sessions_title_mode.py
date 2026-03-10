"""Add title and mode columns to sessions table.

Revision ID: 0018
Revises: 0017
Create Date: 2026-03-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("title", sa.String(512), nullable=True, server_default="New Chat"),
    )
    op.add_column(
        "sessions",
        sa.Column("mode", sa.String(32), nullable=True, server_default="chat"),
    )


def downgrade() -> None:
    op.drop_column("sessions", "mode")
    op.drop_column("sessions", "title")
