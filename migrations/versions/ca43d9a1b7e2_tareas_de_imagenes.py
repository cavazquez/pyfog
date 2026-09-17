"""Add durable image tasks, attempts and progress events."""

import sqlalchemy as sa
from alembic import op

revision = "ca43d9a1b7e2"  # pragma: allowlist secret
down_revision = "8d7f2e0bb2a1"  # pragma: allowlist secret
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_by", sa.Integer(), nullable=False),
        sa.Column("host_id", sa.String(length=36), nullable=False),
        sa.Column("image_id", sa.String(length=36), nullable=False),
        sa.Column("inventory_report_id", sa.String(length=36), nullable=False),
        sa.Column("disk_selector", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("reservation_key", sa.String(length=36), nullable=True),
        sa.Column("transfer_slot", sa.Integer(), nullable=True),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("bytes_processed", sa.BigInteger(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("failure_reason", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["host_id"], ["hosts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["image_id"], ["images.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["inventory_report_id"], ["inventory_reports.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "operation IN ('capture', 'restore', 'clone')", name="ck_tasks_operation"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'assigned', 'running', 'verifying', "
            "'succeeded', 'failed', 'cancelled', 'intervention_required')",
            name="ck_tasks_status",
        ),
        sa.CheckConstraint("bytes_processed >= 0", name="ck_tasks_bytes_processed"),
        sa.CheckConstraint("total_bytes IS NULL OR total_bytes >= 0", name="ck_tasks_total_bytes"),
        sa.UniqueConstraint("idempotency_key", name="uq_tasks_idempotency_key"),
        sa.UniqueConstraint("reservation_key", name="uq_tasks_reservation_key"),
        sa.UniqueConstraint("transfer_slot", name="uq_tasks_transfer_slot"),
    )
    op.create_index("ix_tasks_created_at", "tasks", ["created_at"])
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_host_id", "tasks", ["host_id"])
    op.create_index("ix_tasks_image_id", "tasks", ["image_id"])
    op.create_index("ix_tasks_status_created", "tasks", ["status", "created_at"])
    op.create_index("ix_tasks_host_status", "tasks", ["host_id", "status"])

    op.create_table(
        "task_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("agent_session_id", sa.String(length=36), nullable=False),
        sa.Column("capability_hash", sa.String(length=64), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("bytes_processed", sa.BigInteger(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("assigned_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "attempt_number", name="uq_task_attempt_number"),
        sa.UniqueConstraint("agent_session_id", name="uq_task_agent_session"),
    )
    op.create_index("ix_task_attempts_task_id", "task_attempts", ["task_id"])
    op.create_index("ix_task_attempt_lease", "task_attempts", ["lease_expires_at"])

    op.create_table(
        "task_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("bytes_processed", sa.BigInteger(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["attempt_id"], ["task_attempts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attempt_id", "sequence", name="uq_task_event_sequence"),
    )
    op.create_index("ix_task_events_task_id", "task_events", ["task_id"])
    op.create_index("ix_task_events_created_at", "task_events", ["created_at"])
    op.create_index("ix_task_events_task_created", "task_events", ["task_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_task_events_task_created", table_name="task_events")
    op.drop_index("ix_task_events_created_at", table_name="task_events")
    op.drop_index("ix_task_events_task_id", table_name="task_events")
    op.drop_table("task_events")
    op.drop_index("ix_task_attempt_lease", table_name="task_attempts")
    op.drop_index("ix_task_attempts_task_id", table_name="task_attempts")
    op.drop_table("task_attempts")
    op.drop_index("ix_tasks_host_status", table_name="tasks")
    op.drop_index("ix_tasks_status_created", table_name="tasks")
    op.drop_index("ix_tasks_image_id", table_name="tasks")
    op.drop_index("ix_tasks_host_id", table_name="tasks")
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_index("ix_tasks_created_at", table_name="tasks")
    op.drop_table("tasks")
