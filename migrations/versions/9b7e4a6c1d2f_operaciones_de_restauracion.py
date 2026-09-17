"""Add cancellation metadata for restore and clone operations."""

import sqlalchemy as sa
from alembic import op

revision = "9b7e4a6c1d2f"  # pragma: allowlist secret
down_revision = "ca43d9a1b7e2"  # pragma: allowlist secret
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("cancel_requested_at", sa.DateTime(), nullable=True))
    op.add_column("tasks", sa.Column("cancel_acknowledged_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "cancel_acknowledged_at")
    op.drop_column("tasks", "cancel_requested_at")
