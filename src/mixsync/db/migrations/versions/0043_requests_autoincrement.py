"""requests.id AUTOINCREMENT on SQLite: a deleted request's id is never reused (#428)

Revision ID: 0043_requests_autoincrement
Revises: 0042_mirrored_spotify_import
"""

import sqlalchemy as sa
from alembic import op

revision = "0043_requests_autoincrement"
down_revision: str | None = "0042_mirrored_spotify_import"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":  # Postgres sequences never reuse values
        return
    # The rebuild drops `requests`, which follow_hits / provisional_holds reference; SQLite only
    # lets foreign keys be switched off outside a transaction. Ids are copied unchanged.
    ctx = op.get_context()
    with ctx.autocommit_block():
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    with op.batch_alter_table(
        "requests", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ):
        pass
    # ids already orphaned by past deletes are still referenced by tracks.request_id / batch_id
    top = bind.execute(
        sa.text(
            "SELECT MAX(m) FROM (SELECT MAX(id) AS m FROM requests"
            " UNION ALL SELECT MAX(request_id) FROM tracks)"
        )
    ).scalar()
    for (batch,) in bind.execute(
        sa.text("SELECT DISTINCT batch_id FROM tracks WHERE batch_id LIKE 'request-%'")
    ):
        digits = batch[len("request-") :].split("-", 1)[0]
        if digits.isdigit():
            top = max(top or 0, int(digits))
    if top:
        bind.execute(sa.text("DELETE FROM sqlite_sequence WHERE name = 'requests'"))
        bind.execute(
            sa.text("INSERT INTO sqlite_sequence (name, seq) VALUES ('requests', :n)"), {"n": top}
        )
    with ctx.autocommit_block():  # commits the rebuild, then restores enforcement
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    """No-op: AUTOINCREMENT is harmless on the old schema and the rebuild is not worth repeating."""
