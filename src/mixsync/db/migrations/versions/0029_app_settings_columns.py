"""app_settings: one nullable column per app setting; NULL means not saved (ADR 0021)

Revision ID: 0029_app_settings_columns
Revises: 0028_worker_heartbeats
"""

import sqlalchemy as sa
from alembic import op

revision = "0029_app_settings_columns"
down_revision: str | None = "0028_worker_heartbeats"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("mb_contact", sa.Text()),
    ("mb_rate", sa.Float()),
    ("mb_base_url", sa.Text()),
    ("deezer_suggestions", sa.Boolean()),
    ("discogs_suggestions", sa.Boolean()),
    ("slskd_url", sa.Text()),
    ("slskd_max_searches", sa.Integer()),
    ("slskd_search_debounce", sa.Integer()),
    ("max_concurrent_downloads", sa.Integer()),
    ("slow_download_window", sa.Integer()),
    ("slow_download_floor", sa.Integer()),
    ("slow_download_share", sa.Float()),
    ("navidrome_url", sa.Text()),
    ("navidrome_user", sa.Text()),
    ("navidrome_library", sa.Text()),
    ("navidrome_discover_library", sa.Text()),
    ("adopt_downloads", sa.Boolean()),
    ("adopt_settle_minutes", sa.Integer()),
    ("flac_level", sa.Integer()),
    ("canonical_media", sa.JSON()),
    ("canonical_countries", sa.JSON()),
    ("trash_days", sa.Integer()),
    ("timezone", sa.Text()),
    ("path_template", sa.Text()),
    ("log_level", sa.Text()),
)


def upgrade() -> None:
    with op.batch_alter_table("app_settings") as b:  # an existing row keeps every setting unsaved
        for name, type_ in _COLUMNS:
            b.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("app_settings") as b:
        for name, _ in reversed(_COLUMNS):
            b.drop_column(name)
