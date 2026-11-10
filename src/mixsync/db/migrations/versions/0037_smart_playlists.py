"""smart playlists: rules over the owned library, rebuilt on a cadence

Revision ID: 0037_smart_playlists
Revises: 0036_user_download_auto
"""

import sqlalchemy as sa
from alembic import op

revision = "0037_smart_playlists"
down_revision: str | None = "0036_user_download_auto"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "smart_playlists",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sort", sa.String(16), nullable=False),
        sa.Column("cadence_days", sa.Integer(), nullable=False),
        sa.Column("navidrome_playlist_id", sa.String(64), nullable=True),
        sa.Column("next_build_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_built_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entries", sa.JSON(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_smart_playlists"),
        sa.UniqueConstraint("user_id", "name", name="uq_smart_playlists_user_id"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_smart_playlists_user_id_users",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("smart_playlists")
