"""worker_heartbeats: the app settings version each worker loaded at startup

Revision ID: 0033_worker_settings_version
Revises: 0032_mirror_auto_request
"""

import sqlalchemy as sa
from alembic import op

revision = "0033_worker_settings_version"
down_revision: str | None = "0032_mirror_auto_request"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("worker_heartbeats") as batch:
        batch.add_column(sa.Column("settings_version", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("worker_heartbeats") as batch:
        batch.drop_column("settings_version")
