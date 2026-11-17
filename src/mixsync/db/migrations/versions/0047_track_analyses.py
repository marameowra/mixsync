"""track_analyses: per-track audio analysis, and the audio_analysis app setting (#447, ADR 0025)

Revision ID: 0047_track_analyses
Revises: 0046_torrent_pack_folders
"""

import sqlalchemy as sa
from alembic import op

revision = "0047_track_analyses"
down_revision: str | None = "0046_torrent_pack_folders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "track_analyses",
        sa.Column("track_id", sa.Integer(), nullable=False),
        sa.Column("analyzer", sa.String(length=64), nullable=False),
        sa.Column("feature_version", sa.Integer(), nullable=False),
        sa.Column("bpm", sa.Float(), nullable=True),
        sa.Column("key_tonic", sa.String(length=2), nullable=True),
        sa.Column("key_mode", sa.String(length=5), nullable=True),
        sa.Column("energy", sa.Float(), nullable=True),
        sa.Column("vector", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["track_id"],
            ["tracks.id"],
            name="fk_track_analyses_track_id_tracks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("track_id", name="pk_track_analyses"),
    )
    op.create_index("ix_track_analyses_feature_version", "track_analyses", ["feature_version"])
    with op.batch_alter_table("app_settings") as batch:
        batch.add_column(sa.Column("audio_analysis", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.drop_column("audio_analysis")
    op.drop_index("ix_track_analyses_feature_version", table_name="track_analyses")
    op.drop_table("track_analyses")
