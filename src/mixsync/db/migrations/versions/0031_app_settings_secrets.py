"""app settings: encrypted columns for the saved secrets

Revision ID: 0031_app_settings_secrets
Revises: 0030_mirrored_playlists
"""

import sqlalchemy as sa
from alembic import op

revision = "0031_app_settings_secrets"
down_revision: str | None = "0030_mirrored_playlists"
branch_labels = None
depends_on = None

_COLUMNS = (
    "slskd_api_key_encrypted",
    "navidrome_password_encrypted",
    "acoustid_app_key_encrypted",
    "acoustid_user_key_encrypted",
)


def upgrade() -> None:
    with op.batch_alter_table("app_settings") as b:  # an existing row keeps every secret unsaved
        for name in _COLUMNS:
            b.add_column(sa.Column(name, sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("app_settings") as b:
        for name in reversed(_COLUMNS):
            b.drop_column(name)
