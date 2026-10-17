"""requests: dead_end and partial_note as data, not text in last_error

Revision ID: 0018_request_lifecycle
Revises: 0017_jobs_status_index
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_request_lifecycle"
down_revision: str | None = "0017_jobs_status_index"
branch_labels = None
depends_on = None

PARTIAL = "partial: "


def upgrade() -> None:
    with op.batch_alter_table("requests") as b:
        b.add_column(sa.Column("dead_end", sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("partial_note", sa.Text(), nullable=True))
    c = op.get_bind()
    # backfill from the text the pipeline wrote before these columns existed
    c.execute(
        sa.text(
            "UPDATE requests SET dead_end = :yes WHERE provisional"
            " AND status IN ('failed', 'imported')"
            " AND (last_error LIKE 'none of the wanted recordings%'"
            " OR (status = 'imported' AND last_error LIKE '%not on this release'))"
        ),
        {"yes": True},
    )
    rows = c.execute(sa.text("SELECT id, last_error FROM requests WHERE status = 'partial'"))
    for rid, err in rows.all():
        head = (err or "").split("; ")[0]
        if head.startswith(PARTIAL):
            c.execute(
                sa.text("UPDATE requests SET partial_note = :n WHERE id = :i"),
                {"n": head[len(PARTIAL) :], "i": rid},
            )


def downgrade() -> None:
    with op.batch_alter_table("requests") as b:
        b.drop_column("partial_note")
        b.drop_column("dead_end")
