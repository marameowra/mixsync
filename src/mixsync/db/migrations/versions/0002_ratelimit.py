"""rate_buckets and http_cache

Revision ID: 0002_ratelimit
Revises: 0001_initial
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_ratelimit"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_buckets",
        sa.Column("service", sa.String(32), primary_key=True),
        sa.Column("tokens", sa.Float(), nullable=False),
        sa.Column("capacity", sa.Float(), nullable=False),
        sa.Column("refill_per_sec", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "http_cache",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("service", sa.String(32), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("headers", sa.JSON(), nullable=False),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("http_cache")
    op.drop_table("rate_buckets")
