"""app setting embed_cover (ADR 0021)

Revision ID: 0034_app_settings_embed_cover
Revises: 0033_worker_settings_version
"""

import sqlalchemy as sa
from alembic import op

revision = "0034_app_settings_embed_cover"
down_revision: str | None = "0033_worker_settings_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.add_column(sa.Column("embed_cover", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.drop_column("embed_cover")
