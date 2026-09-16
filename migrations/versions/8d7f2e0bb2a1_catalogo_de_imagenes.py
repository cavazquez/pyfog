"""Add image catalog metadata."""

import sqlalchemy as sa
from alembic import op

revision = "8d7f2e0bb2a1"
down_revision = "6f49b8a1e1d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "images",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("manifest_image_id", sa.String(length=36), nullable=True),
        sa.Column("manifest_json", sa.JSON(), nullable=True),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("source_host_id", sa.String(length=36), nullable=True),
        sa.Column("source_hostname", sa.String(length=253), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=True),
        sa.Column("total_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("compatibility", sa.String(length=500), nullable=False),
        sa.Column("integrity_verified_at", sa.DateTime(), nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["source_host_id"], ["hosts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('draft', 'capturing', 'ready', 'failed')", name="ck_images_status"
        ),
        sa.CheckConstraint(
            "total_size_bytes IS NULL OR total_size_bytes >= 0", name="ck_images_size"
        ),
        sa.UniqueConstraint("manifest_image_id"),
        sa.UniqueConstraint("name"),
    )
    with op.batch_alter_table("images", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_images_status"), ["status"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_images_source_host_id"), ["source_host_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_images_created_at"), ["created_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("images", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_images_created_at"))
        batch_op.drop_index(batch_op.f("ix_images_source_host_id"))
        batch_op.drop_index(batch_op.f("ix_images_status"))
    op.drop_table("images")
