"""Add missing columns to email_queue table (to_email, subject, html, retry_count).

The original migration 0006 created email_queue with a minimal schema.
The EmailQueue ORM model requires these additional columns.

Revision ID: 0014
Revises: 0013
Create Date: 2026-03-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add to_email — use server_default so any pre-existing rows don't violate NOT NULL.
    # After backfill (none expected in dev) the server_default can be removed in a future migration.
    op.add_column(
        "email_queue",
        sa.Column(
            "to_email",
            sa.String(512),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "email_queue",
        sa.Column(
            "subject",
            sa.String(512),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "email_queue",
        sa.Column(
            "html",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "email_queue",
        sa.Column(
            "retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    # Drop the server defaults now that columns exist (they stay NOT NULL).
    op.alter_column("email_queue", "to_email", server_default=None)
    op.alter_column("email_queue", "subject", server_default=None)
    op.alter_column("email_queue", "html", server_default=None)
    op.alter_column("email_queue", "retry_count", server_default=None)


def downgrade() -> None:
    op.drop_column("email_queue", "retry_count")
    op.drop_column("email_queue", "html")
    op.drop_column("email_queue", "subject")
    op.drop_column("email_queue", "to_email")
