"""jobs: index on (status, run_after), for the claim and reap scans

Revision ID: 0017_jobs_status_index
Revises: 0016_provisional_title
"""

from alembic import op

revision = "0017_jobs_status_index"
down_revision: str | None = "0016_provisional_title"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_jobs_status_run_after", "jobs", ["status", "run_after"])


def downgrade() -> None:
    op.drop_index("ix_jobs_status_run_after", "jobs")
