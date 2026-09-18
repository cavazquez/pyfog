"""Add explicit user roles and access-decision audit fields."""

import sqlalchemy as sa
from alembic import op

revision = "b7c4d2e1f906"  # pragma: allowlist secret
down_revision = "4e8c2b7a9d10"  # pragma: allowlist secret
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("role", sa.String(length=16), nullable=False, server_default="admin")
        )
        batch_op.create_check_constraint(
            "ck_users_role", "role IN ('admin', 'operator', 'auditor')"
        )

    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("decision", sa.String(length=16), nullable=False, server_default="allow")
        )
        batch_op.add_column(
            sa.Column("reason", sa.String(length=500), nullable=False, server_default="")
        )
        batch_op.create_check_constraint("ck_audit_decision", "decision IN ('allow', 'deny')")


def downgrade() -> None:
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.drop_constraint("ck_audit_decision", type_="check")
        batch_op.drop_column("reason")
        batch_op.drop_column("decision")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint("ck_users_role", type_="check")
        batch_op.drop_column("role")
