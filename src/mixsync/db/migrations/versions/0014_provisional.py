"""provisional (discovery) requests and their tracks

Revision ID: 0014_provisional
Revises: 0013_user_links
"""

from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "0014_provisional"
down_revision: str | None = "0013_user_links"
branch_labels = None
depends_on = None


def _stamps() -> list[sa.Column[datetime]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.add_column(
        "requests",
        sa.Column("provisional", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "requests",
        sa.Column("recording_mbids", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.create_table(
        "provisional_tracks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("recording_mbid", sa.String(36), nullable=False),
        sa.Column("release_mbid", sa.String(36), nullable=True),
        sa.Column("release_group_mbid", sa.String(36), nullable=True),
        sa.Column("acoustid_id", sa.String(36), nullable=True),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("navidrome_song_id", sa.String(64), nullable=True),
        sa.Column("promoted_path", sa.Text(), nullable=True),
        sa.Column("trash_path", sa.Text(), nullable=True),
        *_stamps(),
        sa.PrimaryKeyConstraint("id", name="pk_provisional_tracks"),
    )
    op.create_index(
        "uq_provisional_tracks_path",
        "provisional_tracks",
        ["path"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_provisional_tracks_recording_mbid", "provisional_tracks", ["recording_mbid"]
    )


def downgrade() -> None:
    op.drop_table("provisional_tracks")
    with op.batch_alter_table("requests") as b:
        b.drop_column("recording_mbids")
        b.drop_column("provisional")
