"""app settings: Prowlarr, qBittorrent and SABnzbd connections, secrets encrypted (#408)

Revision ID: 0038_app_settings_torrent_usenet
Revises: 0037_smart_playlists
"""

import sqlalchemy as sa
from alembic import op

revision = "0038_app_settings_torrent_usenet"
down_revision: str | None = "0037_smart_playlists"
branch_labels = None
depends_on = None

_COLUMNS = (
    "prowlarr_url",
    "qbittorrent_url",
    "qbittorrent_username",
    "sabnzbd_url",
    "prowlarr_api_key_encrypted",
    "qbittorrent_password_encrypted",
    "sabnzbd_api_key_encrypted",
)


def upgrade() -> None:
    with op.batch_alter_table("app_settings") as b:  # an existing row keeps each one unsaved
        for name in _COLUMNS:
            b.add_column(sa.Column(name, sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("app_settings") as b:
        for name in reversed(_COLUMNS):
            b.drop_column(name)
