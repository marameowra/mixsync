"""operation journal: file_ops, tag_snapshots

Revision ID: 0002_journal
Revises: 0001_initial
"""

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0002_journal"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column[Any]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "file_ops",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("src", sa.Text(), nullable=False),
        sa.Column("dst", sa.Text(), nullable=False),
        sa.Column("src_hash", sa.String(128), nullable=True),
        sa.Column("dst_hash", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_file_ops"),
    )
    op.create_index("ix_file_ops_status", "file_ops", ["status"])
    op.create_index("ix_file_ops_batch_id", "file_ops", ["batch_id"])
    op.create_table(
        "tag_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=True),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("op_id", sa.Integer(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["op_id"], ["file_ops.id"], name="fk_tag_snapshots_op_id_file_ops"),
        sa.PrimaryKeyConstraint("id", name="pk_tag_snapshots"),
    )


def downgrade() -> None:
    op.drop_table("tag_snapshots")
    op.drop_index("ix_file_ops_batch_id", "file_ops")
    op.drop_index("ix_file_ops_status", "file_ops")
    op.drop_table("file_ops")
