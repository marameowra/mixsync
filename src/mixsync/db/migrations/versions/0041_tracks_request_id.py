"""tracks.request_id and an index on jobs' payload request_id (#424)

Revision ID: 0041_tracks_request_id
Revises: 0040_http_cache_expires_index
"""

import re

import sqlalchemy as sa
from alembic import op

revision = "0041_tracks_request_id"
down_revision: str | None = "0040_http_cache_expires_index"
branch_labels = None
depends_on = None

_jobs = sa.table("jobs", sa.column("payload", sa.JSON))
# The same expression as models.work.JOB_REQUEST_ID, with the key as a literal.
_JOB_REQUEST_ID = _jobs.c.payload[
    sa.bindparam("request_id_key", "request_id", sa.JSON.JSONStrIndexType(), literal_execute=True)
].as_integer()


def upgrade() -> None:
    with op.batch_alter_table("tracks") as batch:
        batch.add_column(sa.Column("request_id", sa.Integer(), nullable=True))
        batch.create_index("ix_tracks_request_id", ["request_id"])
    tracks = sa.table("tracks", sa.column("id", sa.Integer), sa.column("batch_id", sa.String))
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(tracks.c.id, tracks.c.batch_id).where(tracks.c.batch_id.like("request-%"))
    ).all()
    found = [
        {"tid": tid, "rid": int(m[1])}
        for tid, batch in rows
        if (m := re.match(r"request-(\d+)-", batch))
    ]
    if found:
        bind.execute(
            sa.text("UPDATE tracks SET request_id = :rid WHERE id = :tid"),
            found,
        )
    op.create_index("ix_jobs_request_id", "jobs", [_JOB_REQUEST_ID])


def downgrade() -> None:
    op.drop_index("ix_jobs_request_id", table_name="jobs")
    with op.batch_alter_table("tracks") as batch:
        batch.drop_index("ix_tracks_request_id")
        batch.drop_column("request_id")
