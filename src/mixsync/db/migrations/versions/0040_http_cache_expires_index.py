"""http_cache: index on expires_at, so the purge never scans the bodies (#422)

Revision ID: 0040_http_cache_expires_index
Revises: 0039_app_settings_spotify
"""

from alembic import op

revision = "0040_http_cache_expires_index"
down_revision: str | None = "0039_app_settings_spotify"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_http_cache_expires_at", "http_cache", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_http_cache_expires_at", "http_cache")
