"""Create telemetry_events table (monthly range partitioned).

Revision ID: 0005
Revises: 0004
Create Date: 2026-02-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the parent partitioned table
    op.execute("""
        CREATE TABLE IF NOT EXISTS telemetry_events (
            id              UUID            NOT NULL,
            user_id         UUID            REFERENCES users(id) ON DELETE SET NULL,
            session_id      UUID            REFERENCES sessions(id) ON DELETE SET NULL,
            event_type      VARCHAR(100)    NOT NULL,
            payload         JSONB           NOT NULL DEFAULT '{}',
            ip_address      INET,
            geo             JSONB,
            created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
    """)

    op.create_index("ix_telemetry_events_user_id", "telemetry_events", ["user_id"])
    op.create_index("ix_telemetry_events_event_type", "telemetry_events", ["event_type"])
    op.create_index("ix_telemetry_events_created_at", "telemetry_events", ["created_at"])

    # Create initial monthly partitions for 2026
    for month in range(1, 13):
        start = f"2026-{month:02d}-01"
        end_month = month + 1 if month < 12 else 1
        end_year = 2026 if month < 12 else 2027
        end = f"{end_year}-{end_month:02d}-01"
        part_name = f"telemetry_events_2026_{month:02d}"
        op.execute(f"""
            CREATE TABLE IF NOT EXISTS {part_name}
            PARTITION OF telemetry_events
            FOR VALUES FROM ('{start}') TO ('{end}')
        """)


def downgrade() -> None:
    for month in range(1, 13):
        op.execute(f"DROP TABLE IF EXISTS telemetry_events_2026_{month:02d}")
    op.drop_index("ix_telemetry_events_created_at", table_name="telemetry_events")
    op.drop_index("ix_telemetry_events_event_type", table_name="telemetry_events")
    op.drop_index("ix_telemetry_events_user_id", table_name="telemetry_events")
    op.drop_table("telemetry_events")
