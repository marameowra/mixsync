"""tracks.album_artist, album, year: the file's album tags, filled by a background job

Revision ID: 0023_track_album_names
Revises: 0022_request_content_key
"""

import sqlalchemy as sa
from alembic import op

revision = "0023_track_album_names"
down_revision: str | None = "0022_request_content_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tracks") as b:  # no file is read here: the rows stay null
        b.add_column(sa.Column("album_artist", sa.Text(), nullable=True))
        b.add_column(sa.Column("album", sa.Text(), nullable=True))
        b.add_column(sa.Column("year", sa.String(length=4), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tracks") as b:
        b.drop_column("year")
        b.drop_column("album")
        b.drop_column("album_artist")
