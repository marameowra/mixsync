"""plan_progress: which ops of a plan are done, one small row each, so a run never rewrites the
plan's ops blob per op. Plans from before keep their `done` flags inside `ops`.

Revision ID: 0027_plan_progress
Revises: 0026_track_path_template
"""

import sqlalchemy as sa
from alembic import op

revision = "0027_plan_progress"
down_revision: str | None = "0026_track_path_template"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plan_progress",
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("op_index", sa.Integer(), nullable=False),
        sa.Column("done", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"], name="fk_plan_progress_plan_id_plans"),
        sa.PrimaryKeyConstraint("plan_id", "op_index", name="pk_plan_progress"),
    )


def downgrade() -> None:
    op.drop_table("plan_progress")
