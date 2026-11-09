"""app setting source_priority: the admin's ordered download sources (#324)

Revision ID: 0035_app_settings_sources
Revises: 0034_app_settings_embed_cover
"""

import sqlalchemy as sa
from alembic import op

revision = "0035_app_settings_sources"
down_revision: str | None = "0034_app_settings_embed_cover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.add_column(sa.Column("source_priority", sa.JSON(none_as_null=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.drop_column("source_priority")
