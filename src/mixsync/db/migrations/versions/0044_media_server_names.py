"""media server kind and generic names: navidrome_* -> media_server_*, server ids (#440)

Revision ID: 0044_media_server_names
Revises: 0043_requests_autoincrement

Renames keep every value. user_links.service needs no change: "navidrome" is the kind's value.
"""

import sqlalchemy as sa
from alembic import op

revision = "0044_media_server_names"
down_revision: str | None = "0043_requests_autoincrement"
branch_labels = None
depends_on = None

# (table, old name, new name)
RENAMES = (
    ("app_settings", "navidrome_url", "media_server_url"),
    ("app_settings", "navidrome_user", "media_server_user"),
    ("app_settings", "navidrome_library", "media_server_library"),
    ("app_settings", "navidrome_discover_library", "media_server_discover_library"),
    ("app_settings", "navidrome_password_encrypted", "media_server_password_encrypted"),
    ("discovery_playlists", "navidrome_playlist_id", "server_playlist_id"),
    ("mirrored_playlists", "navidrome_playlist_id", "server_playlist_id"),
    ("smart_playlists", "navidrome_playlist_id", "server_playlist_id"),
    ("provisional_tracks", "navidrome_song_id", "server_song_id"),
)


def _rename(backwards: bool) -> None:
    # RENAME COLUMN in place on both SQLite and Postgres; a batch recreate would drop and copy
    # tables other rows reference
    for table, old, new in RENAMES:
        a, b = (new, old) if backwards else (old, new)
        op.alter_column(table, a, new_column_name=b)


def upgrade() -> None:
    _rename(backwards=False)
    op.add_column("app_settings", sa.Column("media_server_kind", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.drop_column("media_server_kind")
    _rename(backwards=True)
