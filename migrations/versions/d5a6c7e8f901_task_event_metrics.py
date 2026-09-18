"""Add bounded metrics to task events."""

import sqlalchemy as sa
from alembic import op

revision = "d5a6c7e8f901"  # pragma: allowlist secret
down_revision = "c4e8a1b7d932"  # pragma: allowlist secret
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task_events", sa.Column("duration_ms", sa.BigInteger(), nullable=True))
    op.add_column(
        "task_events",
        sa.Column("throughput_bytes_per_second", sa.BigInteger(), nullable=True),
    )
    op.add_column("task_events", sa.Column("failure_code", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_task_events_type_created",
        "task_events",
        ["event_type", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_task_events_failure_created",
        "task_events",
        ["failure_code", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_task_events_failure_created", table_name="task_events")
    op.drop_index("ix_task_events_type_created", table_name="task_events")
    with op.batch_alter_table("task_events", schema=None) as batch_op:
        batch_op.drop_column("failure_code")
        batch_op.drop_column("throughput_bytes_per_second")
        batch_op.drop_column("duration_ms")
