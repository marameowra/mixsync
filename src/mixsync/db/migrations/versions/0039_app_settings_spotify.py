"""app setting spotify_client_id (#337)

Revision ID: 0039_app_settings_spotify
Revises: 0038_app_settings_torrent_usenet
"""

import sqlalchemy as sa
from alembic import op

revision = "0039_app_settings_spotify"
down_revision: str | None = "0038_app_settings_torrent_usenet"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.add_column(sa.Column("spotify_client_id", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.drop_column("spotify_client_id")
