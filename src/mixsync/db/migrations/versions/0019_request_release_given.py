"""requests: whether the release was given or identified, so a re-check need not read old jobs

Revision ID: 0019_request_release_given
Revises: 0018_request_lifecycle
"""

import sqlalchemy as sa
from alembic import op

revision = "0019_request_release_given"
down_revision: str | None = "0018_request_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("requests") as b:
        b.add_column(
            sa.Column("release_given", sa.Boolean(), nullable=False, server_default=sa.true())
        )
    # The last VERIFY of a request says which it was; none at all: only a migration identified.
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
        sa.column("release_given", sa.Boolean),
    )
    last: dict[int, bool] = {}
    for (payload,) in bind.execute(
        sa.select(jobs.c.payload).where(jobs.c.kind == "verify").order_by(jobs.c.id)
    ):
        last[payload["request_id"]] = bool(payload.get("release_mbid"))
    bind.execute(
        requests.update().where(requests.c.migration_id.is_not(None)).values(release_given=False)
    )
    for rid, given in last.items():
        bind.execute(requests.update().where(requests.c.id == rid).values(release_given=given))


def downgrade() -> None:
    with op.batch_alter_table("requests") as b:
        b.drop_column("release_given")
