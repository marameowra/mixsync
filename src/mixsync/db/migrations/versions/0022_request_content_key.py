"""requests.content_key: a drop-folder album's key, so the drop folder reads no VERIFY jobs

Revision ID: 0022_request_content_key
Revises: 0021_request_budget
"""

import re

import sqlalchemy as sa
from alembic import op

revision = "0022_request_content_key"
down_revision: str | None = "0021_request_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("requests") as b:
        b.add_column(sa.Column("content_key", sa.String(length=80), nullable=True))
        b.create_index("ix_requests_content_key", ["content_key"])
    # A drop/adopt request: its VERIFY job's idempotency key without the retry's ":<n>".
    # One with no VERIFY job stays null.
    bind = op.get_bind()
    jobs = sa.table(
        "jobs",
        sa.column("kind", sa.String),
        sa.column("payload", sa.JSON),
        sa.column("idempotency_key", sa.String),
    )
    requests = sa.table(
        "requests",
        sa.column("id", sa.Integer),
        sa.column("source", sa.String),
        sa.column("content_key", sa.String),
    )
    ours = {
        rid
        for (rid,) in bind.execute(
            sa.select(requests.c.id).where(requests.c.source.in_(("drop", "adopt")))
        )
    }
    for payload, key in bind.execute(
        sa.select(jobs.c.payload, jobs.c.idempotency_key).where(
            jobs.c.kind == "verify", jobs.c.idempotency_key.is_not(None)
        )
    ).all():
        rid = payload["request_id"]
        if rid in ours:
            bind.execute(
                requests.update()
                .where(requests.c.id == rid)
                .values(content_key=re.sub(r":\d+$", "", key))
            )


def downgrade() -> None:
    with op.batch_alter_table("requests") as b:
        b.drop_index("ix_requests_content_key")
        b.drop_column("content_key")
