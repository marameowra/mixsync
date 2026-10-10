"""plans table

Revision ID: 0007_plans
Revises: 0006_canonical_releases
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_plans"
down_revision: str | None = "0006_canonical_releases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("release_group_mbid", sa.String(36), nullable=False),
        sa.Column("canonical_release_mbid", sa.String(36), nullable=False),
        sa.Column("previous_canonical_mbid", sa.String(36), nullable=True),
        sa.Column("canonical_set", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("ops", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_plans"),
    )


def downgrade() -> None:
    op.drop_table("plans")
