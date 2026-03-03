"""Create users table.

Revision ID: 0001
Revises:
Create Date: 2026-02-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("clerk_id", sa.String(255), nullable=True),
        sa.Column("github_login", sa.String(255), nullable=True),
        sa.Column("email", sa.String(512), nullable=True),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("plan", sa.String(20), nullable=False, server_default="free"),
        sa.Column("trial_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trial_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trial_days_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("account_deleted", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clerk_id", name="uq_users_clerk_id"),
    )
    op.create_index("ix_users_github_login", "users", ["github_login"])


def downgrade() -> None:
    op.drop_index("ix_users_github_login", table_name="users")
    op.drop_table("users")
