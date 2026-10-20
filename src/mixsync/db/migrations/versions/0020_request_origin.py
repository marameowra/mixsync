"""requests.origin: the candidate and paths a from-files request first verified, so nothing has
to read them back from VERIFY jobs

Revision ID: 0020_request_origin
Revises: 0019_decision_release_given
"""

import sqlalchemy as sa
from alembic import op

revision = "0020_request_origin"
down_revision: str | None = "0019_decision_release_given"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("requests") as b:
        b.add_column(sa.Column("origin", sa.JSON(), nullable=True))
    # A from-files request (migration, drop folder, adopt): its first VERIFY's candidate and
    # paths. One with no VERIFY job stays null.
    bind = op.get_bind()
    jobs = sa.table(
        "jobs",
        sa.column("id", sa.Integer),
        sa.column("kind", sa.String),
        sa.column("payload", sa.JSON),
    )
    requests = sa.table(
        "requests",
        sa.column("id", sa.Integer),
        sa.column("migration_id", sa.Integer),
        sa.column("source", sa.String),
        sa.column("origin", sa.JSON),
    )
    from_files = {
        rid
        for (rid,) in bind.execute(
            sa.select(requests.c.id).where(
                sa.or_(
                    requests.c.migration_id.is_not(None), requests.c.source.in_(("drop", "adopt"))
                )
            )
        )
    }
    first: dict[int, dict[str, object]] = {}
    for (payload,) in bind.execute(
        sa.select(jobs.c.payload).where(jobs.c.kind == "verify").order_by(jobs.c.id)
    ):
        rid = payload["request_id"]
        if rid in from_files and rid not in first:
            first[rid] = {
                "candidate": payload.get("candidate", {}),
                "paths": payload.get("paths", []),
            }
    for rid, origin in first.items():
        bind.execute(requests.update().where(requests.c.id == rid).values(origin=origin))


def downgrade() -> None:
    with op.batch_alter_table("requests") as b:
        b.drop_column("origin")
