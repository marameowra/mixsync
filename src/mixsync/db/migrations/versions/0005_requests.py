"""requests table

Revision ID: 0005_requests
Revises: 0004_match_decisions
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_requests"
down_revision: str | None = "0004_match_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("release_mbid", sa.String(36), nullable=False),
        sa.Column("artist", sa.String(255), nullable=False),
        sa.Column("album", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("tried", sa.JSON(), nullable=False),
        sa.Column("no_results_attempts", sa.Integer(), nullable=False),
        sa.Column("next_search_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_requests"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_requests_user_id_users"),
    )


def downgrade() -> None:
    op.drop_table("requests")
