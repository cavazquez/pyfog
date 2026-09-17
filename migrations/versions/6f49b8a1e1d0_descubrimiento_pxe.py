"""Add short-lived PXE pairing requests."""

import sqlalchemy as sa
from alembic import op

revision = "6f49b8a1e1d0"  # pragma: allowlist secret
down_revision = "041a7f3c068c"  # pragma: allowlist secret
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pairing_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("mac_address", sa.String(length=17), nullable=False),
        sa.Column("challenge", sa.String(length=128), nullable=True),
        sa.Column("challenge_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("host_id", sa.String(length=36), nullable=True),
        sa.Column("capability_hash", sa.String(length=64), nullable=False),
        sa.Column("capability_used_at", sa.DateTime(), nullable=True),
        sa.Column("last_report_id", sa.String(length=36), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(), nullable=True),
        sa.Column("rejection_reason", sa.String(length=500), nullable=False),
        sa.ForeignKeyConstraint(["host_id"], ["hosts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", name="uq_pairing_session"),
    )
    with op.batch_alter_table("pairing_requests", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_pairing_requests_mac_address"), ["mac_address"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_pairing_requests_status"), ["status"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_pairing_requests_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_pairing_requests_expires_at"), ["expires_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_pairing_requests_host_id"), ["host_id"], unique=False)
        batch_op.create_index("ix_pairing_status_expires", ["status", "expires_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("pairing_requests", schema=None) as batch_op:
        batch_op.drop_index("ix_pairing_status_expires")
        batch_op.drop_index(batch_op.f("ix_pairing_requests_host_id"))
        batch_op.drop_index(batch_op.f("ix_pairing_requests_expires_at"))
        batch_op.drop_index(batch_op.f("ix_pairing_requests_created_at"))
        batch_op.drop_index(batch_op.f("ix_pairing_requests_status"))
        batch_op.drop_index(batch_op.f("ix_pairing_requests_mac_address"))
    op.drop_table("pairing_requests")
