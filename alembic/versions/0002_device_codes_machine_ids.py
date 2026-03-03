"""Create device_codes and machine_ids tables.

Revision ID: 0002
Revises: 0001
Create Date: 2026-02-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── device_codes ─────────────────────────────────────────
    op.create_table(
        "device_codes",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("device_id", sa.String(255), nullable=False),
        sa.Column("code", sa.String(6), nullable=False),
        sa.Column("clerk_user_id", sa.String(255), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("machine_id", sa.String(512), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_id", name="uq_device_codes_device_id"),
    )
    op.create_index("ix_device_codes_device_id", "device_codes", ["device_id"])

    # ── machine_ids ───────────────────────────────────────────
    op.create_table(
        "machine_ids",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("machine_id", sa.String(512), nullable=False),
        sa.Column("mac_machine_id", sa.String(512), nullable=True),
        sa.Column("dev_device_id", sa.String(255), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("os_info", postgresql.JSONB(), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "machine_id", name="uq_machine_ids_user_machine"),
    )
    op.create_index("ix_machine_ids_user_id", "machine_ids", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_machine_ids_user_id", table_name="machine_ids")
    op.drop_table("machine_ids")
    op.drop_index("ix_device_codes_device_id", table_name="device_codes")
    op.drop_table("device_codes")
