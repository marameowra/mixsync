"""mirrored_playlists.imported_at and reimport_after: Spotify imports go stale (#416)

Revision ID: 0042_mirrored_spotify_import
Revises: 0041_tracks_request_id
"""

from datetime import timedelta

import sqlalchemy as sa
from alembic import op

revision = "0042_mirrored_spotify_import"
down_revision: str | None = "0041_tracks_request_id"
branch_labels = None
depends_on = None

REIMPORT_FROM = timedelta(days=182 - 14)  # spotify_api.STALE_AFTER - REIMPORT_WINDOW, frozen


def upgrade() -> None:
    op.add_column("mirrored_playlists", sa.Column("imported_at", sa.DateTime(timezone=True)))
    op.add_column("mirrored_playlists", sa.Column("reimport_after", sa.DateTime(timezone=True)))
    # an existing import counts from its first import: the earliest it can have been
    pl = sa.table(
        "mirrored_playlists",
        sa.column("id", sa.Integer),
        sa.column("upstream_kind", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("imported_at", sa.DateTime(timezone=True)),
        sa.column("reimport_after", sa.DateTime(timezone=True)),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(pl.c.id, pl.c.created_at).where(pl.c.upstream_kind == "spotify_api")
    ).all()
    for pid, created in rows:
        bind.execute(
            pl.update()
            .where(pl.c.id == pid)
            .values(imported_at=created, reimport_after=created + REIMPORT_FROM)
        )


def downgrade() -> None:
    with op.batch_alter_table("mirrored_playlists") as batch:
        batch.drop_column("reimport_after")
        batch.drop_column("imported_at")
