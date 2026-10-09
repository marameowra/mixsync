"""tracks table

Revision ID: 0003_tracks
Revises: 0002_jobs
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_tracks"
down_revision: str | None = "0002_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tracks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("recording_mbid", sa.String(36), nullable=True),
        sa.Column("release_mbid", sa.String(36), nullable=True),
        sa.Column("release_group_mbid", sa.String(36), nullable=True),
        sa.Column("artist_mbids", sa.JSON(), nullable=False),
        sa.Column("acoustid_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tracks"),
        sa.UniqueConstraint("path", name="uq_tracks_path"),
    )


def downgrade() -> None:
    op.drop_table("tracks")
