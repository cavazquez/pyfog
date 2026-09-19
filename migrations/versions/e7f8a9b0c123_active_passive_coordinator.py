"""Persist active/passive coordinator leases and fencing tokens."""

import sqlalchemy as sa
from alembic import op

revision = "e7f8a9b0c123"  # pragma: allowlist secret
down_revision = "d5a6c7e8f901"  # pragma: allowlist secret
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coordinator_leases",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("holder_id", sa.String(length=128), nullable=False),
        sa.Column("term", sa.BigInteger(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("coordinator_leases")
