"""Add image deletion metadata and the operational audit log."""

import sqlalchemy as sa
from alembic import op

revision = "4e8c2b7a9d10"  # pragma: allowlist secret
down_revision = "9b7e4a6c1d2f"  # pragma: allowlist secret
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("images", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("actor_host_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=100), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["actor_host_id"], ["hosts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("outcome IN ('success', 'failure')", name="ck_audit_outcome"),
    )
    op.create_index("ix_audit_events_actor_user_id", "audit_events", ["actor_user_id"])
    op.create_index("ix_audit_events_actor_host_id", "audit_events", ["actor_host_id"])
    op.create_index("ix_audit_events_created", "audit_events", ["created_at"])
    op.create_index(
        "ix_audit_events_resource",
        "audit_events",
        ["resource_type", "resource_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_resource", table_name="audit_events")
    op.drop_index("ix_audit_events_created", table_name="audit_events")
    op.drop_index("ix_audit_events_actor_host_id", table_name="audit_events")
    op.drop_index("ix_audit_events_actor_user_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_column("images", "deleted_at")
