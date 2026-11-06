"""worker_heartbeats: when each worker's polling loop last went round

Revision ID: 0026_worker_heartbeats
Revises: 0025_app_settings
"""

import sqlalchemy as sa
from alembic import op

revision = "0026_worker_heartbeats"
down_revision: str | None = "0025_app_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
