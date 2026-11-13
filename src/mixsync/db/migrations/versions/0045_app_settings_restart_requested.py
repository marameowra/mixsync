"""app_settings.restart_requested_at: Apply asks web and worker to restart (#349)

Revision ID: 0045_restart_requested
Revises: 0044_media_server_names
"""

import sqlalchemy as sa
from alembic import op

revision = "0045_restart_requested"
down_revision: str | None = "0044_media_server_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.add_column(sa.Column("restart_requested_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.drop_column("restart_requested_at")
