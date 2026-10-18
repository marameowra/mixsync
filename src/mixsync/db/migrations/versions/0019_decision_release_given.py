"""match_decisions: whether the release was given or identified, so a re-check reads no old jobs

Revision ID: 0019_decision_release_given
Revises: 0018_request_lifecycle
"""

import sqlalchemy as sa
from alembic import op

revision = "0019_decision_release_given"
down_revision: str | None = "0018_request_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("match_decisions") as b:
        b.add_column(
            sa.Column("release_given", sa.Boolean(), nullable=False, server_default=sa.true())
        )
    # What the old re-check computed for the decision's request: its last VERIFY says which it
    # was; none at all: only a migration identified. A decision with no request keeps the default.
    bind = op.get_bind()
    jobs = sa.table(
        "jobs",
        sa.column("id", sa.Integer),
        sa.column("kind", sa.String),
        sa.column("payload", sa.JSON),
    )
    requests = sa.table(
        "requests", sa.column("id", sa.Integer), sa.column("migration_id", sa.Integer)
    )
    decisions = sa.table(
        "match_decisions",
        sa.column("id", sa.Integer),
        sa.column("evidence", sa.JSON),
        sa.column("release_given", sa.Boolean),
    )
    last: dict[int, bool] = {}
    for (payload,) in bind.execute(
        sa.select(jobs.c.payload).where(jobs.c.kind == "verify").order_by(jobs.c.id)
    ):
        last[payload["request_id"]] = bool(payload.get("release_mbid"))
    migrated = {
        rid
        for (rid,) in bind.execute(
            sa.select(requests.c.id).where(requests.c.migration_id.is_not(None))
        )
    }
    for did, evidence in bind.execute(sa.select(decisions.c.id, decisions.c.evidence)).all():
        evidence_: dict[str, int] = evidence or {}
        rid = evidence_.get("request_id")
        if rid is None:
            continue
        given = last.get(rid, rid not in migrated)
        bind.execute(decisions.update().where(decisions.c.id == did).values(release_given=given))


def downgrade() -> None:
    with op.batch_alter_table("match_decisions") as b:
        b.drop_column("release_given")
