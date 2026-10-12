"""discovery playlists and provisional holds

Revision ID: 0015_discovery_playlists
Revises: 0014_provisional
"""

from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "0015_discovery_playlists"
down_revision: str | None = "0014_provisional"
branch_labels = None
depends_on = None


def _stamps() -> list[sa.Column[datetime]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "discovery_playlists",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("unowned_ratio", sa.Float(), nullable=False),
        sa.Column("cadence_days", sa.Integer(), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("keep_plays", sa.Integer(), nullable=False),
        sa.Column("navidrome_playlist_id", sa.String(64), nullable=True),
        sa.Column("next_build_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_built_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entries", sa.JSON(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_stamps(),
        sa.PrimaryKeyConstraint("id", name="pk_discovery_playlists"),
        sa.UniqueConstraint("user_id", "name", name="uq_discovery_playlists_user_id"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_discovery_playlists_user_id_users",
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "provisional_holds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("recording_mbid", sa.String(36), nullable=False),
        sa.Column("playlist_id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=True),
        sa.Column("provisional_track_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("kept_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("keep_reason", sa.String(16), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        sa.PrimaryKeyConstraint("id", name="pk_provisional_holds"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_provisional_holds_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["playlist_id"],
            ["discovery_playlists.id"],
            name="fk_provisional_holds_playlist_id_discovery_playlists",
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["requests.id"], name="fk_provisional_holds_request_id_requests"
        ),
        sa.ForeignKeyConstraint(
            ["provisional_track_id"],
            ["provisional_tracks.id"],
            name="fk_provisional_holds_provisional_track_id_provisional_tracks",
        ),
        sa.UniqueConstraint("user_id", "recording_mbid", name="uq_provisional_holds_user_id"),
    )
    op.create_index("ix_provisional_holds_recording_mbid", "provisional_holds", ["recording_mbid"])


def downgrade() -> None:
    op.drop_table("provisional_holds")
    op.drop_table("discovery_playlists")
