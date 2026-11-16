"""app setting torrent_pack_folders: download single albums from packs (#328, ADR 0026)

Revision ID: 0046_torrent_pack_folders
Revises: 0045_restart_requested
"""

import sqlalchemy as sa
from alembic import op

revision = "0046_torrent_pack_folders"
down_revision: str | None = "0045_restart_requested"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.add_column(sa.Column("torrent_pack_folders", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.drop_column("torrent_pack_folders")
