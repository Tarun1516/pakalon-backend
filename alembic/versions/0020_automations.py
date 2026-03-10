"""automation workflows, connectors, and logs.

Revision ID: 0020_automations
Revises: 0019
Create Date: 2026-03-06 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0020_automations"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "automations",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("template_key", sa.String(length=100), nullable=True),
        sa.Column("inferred_config", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("required_connectors", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("schedule_cron", sa.String(length=100), nullable=True),
        sa.Column("schedule_timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=32), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_automations_user_id", "automations", ["user_id"])

    op.create_table(
        "automation_connectors",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("connection_status", sa.String(length=32), nullable=False, server_default="connected"),
        sa.Column("account_label", sa.String(length=255), nullable=True),
        sa.Column("external_account_id", sa.String(length=255), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "provider", name="uq_automation_connectors_user_provider"),
    )
    op.create_index("ix_automation_connectors_user_id", "automation_connectors", ["user_id"])
    op.create_index("ix_automation_connectors_provider", "automation_connectors", ["provider"])

    op.create_table(
        "automation_logs",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("automation_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("automations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trigger_type", sa.String(length=32), nullable=False, server_default="cron"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_automation_logs_automation_id", "automation_logs", ["automation_id"])
    op.create_index("ix_automation_logs_user_id", "automation_logs", ["user_id"])


def downgrade() -> None:
    op.drop_table("automation_logs")
    op.drop_table("automation_connectors")
    op.drop_table("automations")