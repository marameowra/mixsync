"""users.download_auto: the per-user automatic downloads permission (#296)

Revision ID: 0036_user_download_auto
Revises: 0035_app_settings_sources
"""

import sqlalchemy as sa
from alembic import op

revision = "0036_user_download_auto"
down_revision: str | None = "0035_app_settings_sources"
branch_labels = None
depends_on = None


# Plain ALTER TABLE, not batch: recreating users on SQLite trips the foreign keys pointing at it.
def upgrade() -> None:
    op.add_column(  # existing users keep downloading as today
        "users", sa.Column("download_auto", sa.Boolean(), nullable=False, server_default=sa.true())
    )


def downgrade() -> None:
    op.drop_column("users", "download_auto")
