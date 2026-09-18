"""Add rotatable agent credentials and bind task capabilities to them."""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "c4e8a1b7d932"  # pragma: allowlist secret
down_revision = "b7c4d2e1f906"  # pragma: allowlist secret
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_credentials",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("host_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("grace_until", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["host_id"], ["hosts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_agent_credentials_token_hash"),
    )
    op.create_index("ix_agent_credentials_host_id", "agent_credentials", ["host_id"], unique=False)
    op.create_index(
        "ix_agent_credentials_host_valid",
        "agent_credentials",
        ["host_id", "revoked_at", "expires_at"],
        unique=False,
    )

    connection = op.get_bind()
    legacy_rows = connection.execute(
        sa.text(
            "SELECT id, token_hash, token_expires_at FROM hosts "
            "WHERE token_hash IS NOT NULL AND token_expires_at IS NOT NULL"
        )
    ).mappings()
    credentials = sa.table(
        "agent_credentials",
        sa.column("id", sa.String()),
        sa.column("host_id", sa.String()),
        sa.column("token_hash", sa.String()),
        sa.column("issued_at", sa.DateTime()),
        sa.column("expires_at", sa.DateTime()),
    )
    issued_at = datetime.now(UTC).replace(tzinfo=None)
    for row in legacy_rows:
        connection.execute(
            credentials.insert().values(
                id=str(uuid.uuid4()),
                host_id=row["id"],
                token_hash=row["token_hash"],
                issued_at=issued_at,
                expires_at=row["token_expires_at"],
            )
        )

    op.add_column(
        "task_attempts",
        sa.Column("agent_credential_id", sa.String(length=36), nullable=True),
    )
    with op.batch_alter_table("task_attempts", schema=None) as batch_op:
        batch_op.create_foreign_key(
            "fk_task_attempts_agent_credential",
            "agent_credentials",
            ["agent_credential_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_task_attempts_agent_credential_id", ["agent_credential_id"], unique=False
        )

    connection.execute(
        sa.text(
            """
            UPDATE task_attempts
               SET agent_credential_id = (
                   SELECT ac.id
                     FROM agent_credentials ac
                     JOIN tasks t ON t.host_id = ac.host_id
                    WHERE t.id = task_attempts.task_id
                    ORDER BY ac.issued_at DESC, ac.id DESC
                    LIMIT 1
               )
             WHERE agent_credential_id IS NULL
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("task_attempts", schema=None) as batch_op:
        batch_op.drop_index("ix_task_attempts_agent_credential_id")
        batch_op.drop_constraint("fk_task_attempts_agent_credential", type_="foreignkey")
        batch_op.drop_column("agent_credential_id")
    op.drop_index("ix_agent_credentials_host_valid", table_name="agent_credentials")
    op.drop_index("ix_agent_credentials_host_id", table_name="agent_credentials")
    op.drop_table("agent_credentials")
