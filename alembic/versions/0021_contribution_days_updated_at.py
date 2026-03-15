"""Add contribution_days.updated_at if missing.

Revision ID: 0021_contribution_days_updated_at
Revises: 0020_automations
Create Date: 2026-03-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0021_contribution_days_updated_at"
down_revision: str | None = "0020_automations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("contribution_days")}

    if "updated_at" not in columns:
        op.add_column(
            "contribution_days",
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("contribution_days")}

    if "updated_at" in columns:
        op.drop_column("contribution_days", "updated_at")
