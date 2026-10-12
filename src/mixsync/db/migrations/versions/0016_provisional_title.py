"""provisional_tracks: title and artist, to find the song in Navidrome

Revision ID: 0016_provisional_title
Revises: 0015_discovery_playlists
"""

import sqlalchemy as sa
from alembic import op

revision = "0016_provisional_title"
down_revision: str | None = "0015_discovery_playlists"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("provisional_tracks") as b:
        b.add_column(sa.Column("title", sa.Text(), nullable=True))
        b.add_column(sa.Column("artist", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("provisional_tracks") as b:
        b.drop_column("artist")
        b.drop_column("title")
