"""match_decisions table

Revision ID: 0004_match_decisions
Revises: 0003_tracks
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_match_decisions"
down_revision: str | None = "0003_tracks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "match_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("stage", sa.Integer(), nullable=False),
        sa.Column("release_mbid", sa.String(36), nullable=False),
        sa.Column("band", sa.String(16), nullable=False),
        sa.Column("distance", sa.Float(), nullable=False),
        sa.Column("breakdown", sa.JSON(), nullable=False),
        sa.Column("vetoes", sa.JSON(), nullable=False),
        sa.Column("scorer_version", sa.Integer(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("final_action", sa.String(16), nullable=True),
        sa.Column("chosen_release_mbid", sa.String(36), nullable=True),
        sa.Column("acted_by", sa.Integer(), nullable=True),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_match_decisions"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_match_decisions_user_id_users"),
        sa.ForeignKeyConstraint(
            ["acted_by"], ["users.id"], name="fk_match_decisions_acted_by_users"
        ),
    )


def downgrade() -> None:
    op.drop_table("match_decisions")
