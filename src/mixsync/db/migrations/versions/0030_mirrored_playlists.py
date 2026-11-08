"""mirrored playlists: a user's read-only copy of a playlist curated elsewhere

Revision ID: 0030_mirrored_playlists
Revises: 0029_app_settings_columns
"""

import sqlalchemy as sa
from alembic import op

revision = "0030_mirrored_playlists"
down_revision: str | None = "0029_app_settings_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mirrored_playlists",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("upstream_kind", sa.String(32), nullable=False),
        sa.Column("upstream_ref", sa.String(255), nullable=False),
        sa.Column("cadence_days", sa.Integer(), nullable=False),
        sa.Column("entries", sa.JSON(), nullable=False),
        sa.Column("navidrome_playlist_id", sa.String(64), nullable=True),
        sa.Column("next_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_mirrored_playlists"),
        sa.UniqueConstraint("user_id", "name", name="uq_mirrored_playlists_user_id"),
        sa.UniqueConstraint(
            "user_id", "upstream_kind", "upstream_ref", name="uq_mirrored_playlists_upstream"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_mirrored_playlists_user_id_users",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("mirrored_playlists")
