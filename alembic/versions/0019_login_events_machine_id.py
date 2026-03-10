"""Add machine_id column to login_events table.

Revision ID: 0019
Revises: 0018
Create Date: 2026-03-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "login_events",
        sa.Column("machine_id", sa.String(512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("login_events", "machine_id")
