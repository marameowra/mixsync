"""canonical_releases table

Revision ID: 0006_canonical_releases
Revises: 0005_requests
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_canonical_releases"
down_revision: str | None = "0005_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "canonical_releases",
        sa.Column("release_group_mbid", sa.String(36), nullable=False),
        sa.Column("release_mbid", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("release_group_mbid", name="pk_canonical_releases"),
    )


def downgrade() -> None:
    op.drop_table("canonical_releases")
