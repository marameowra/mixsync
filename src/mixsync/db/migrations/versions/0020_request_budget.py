"""requests: how many candidates each has dropped after bytes landed (the request budget)

Revision ID: 0020_request_budget
Revises: 0019_decision_release_given
"""

import sqlalchemy as sa
from alembic import op

revision = "0020_request_budget"
down_revision: str | None = "0019_decision_release_given"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("requests") as b:
        b.add_column(sa.Column("dropped", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("requests") as b:
        b.drop_column("dropped")
