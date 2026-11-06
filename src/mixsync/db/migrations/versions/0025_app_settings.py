"""app_settings: admin-edited app-wide settings, starting with the auto-accept confidence cap

Revision ID: 0025_app_settings
Revises: 0024_mb_name
"""

import sqlalchemy as sa
from alembic import op

revision = "0025_app_settings"
down_revision: str | None = "0024_mb_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),  # always 1: no sequence
        sa.Column("min_auto_accept_confidence", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
