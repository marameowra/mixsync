"""index on match_decisions.created_at, for the migration report

Revision ID: 0010_decision_created_at_index
Revises: 0009_watchlist
"""

from alembic import op

revision = "0010_decision_created_at_index"
down_revision: str | None = "0009_watchlist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_match_decisions_created_at", "match_decisions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_match_decisions_created_at", table_name="match_decisions")
