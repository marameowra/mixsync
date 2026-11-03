"""mb_name: display names of MusicBrainz ids, kept after the response cache expires

Revision ID: 0024_mb_name
Revises: 0023_track_album_names
"""

import sqlalchemy as sa
from alembic import op

revision = "0024_mb_name"
down_revision: str | None = "0023_track_album_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mb_name",
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("mbid", sa.String(length=36), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("kind", "mbid"),
    )


def downgrade() -> None:
    op.drop_table("mb_name")
