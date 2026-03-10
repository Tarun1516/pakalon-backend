"""Rename clerk_id → supabase_id (users) and clerk_user_id → supabase_user_id (device_codes).

Supabase replaces Clerk as the authentication provider.
All external provider identity columns are renamed to reflect this.

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-01
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rename provider-identity columns from Clerk to Supabase naming."""
    # users.clerk_id → users.supabase_id
    op.alter_column("users", "clerk_id", new_column_name="supabase_id")

    # device_codes.clerk_user_id → device_codes.supabase_user_id
    op.alter_column("device_codes", "clerk_user_id", new_column_name="supabase_user_id")


def downgrade() -> None:
    """Revert column renames back to Clerk naming."""
    op.alter_column("users", "supabase_id", new_column_name="clerk_id")
    op.alter_column("device_codes", "supabase_user_id", new_column_name="clerk_user_id")
