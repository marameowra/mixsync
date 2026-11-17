"""radio_playlists: playlists grown from a seed by sonic similarity (#449)

A Radio seed names a smart, discovery or mirrored playlist by id, so on SQLite those tables
get AUTOINCREMENT, as requests did in 0043: a deleted playlist's id is never handed to the next
one, which would quietly become the seed.

Revision ID: 0048_radio_playlists
Revises: 0047_track_analyses
"""

import sqlalchemy as sa
from alembic import op

revision = "0048_radio_playlists"
down_revision: str | None = "0047_track_analyses"
branch_labels = None
depends_on = None

SEEDS = ("discovery_playlists", "mirrored_playlists", "smart_playlists")


def upgrade() -> None:
    op.create_table(
        "radio_playlists",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("seed_kind", sa.String(16), nullable=False),
        sa.Column("seed_ref", sa.String(255), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("cadence_days", sa.Integer(), nullable=False),
        sa.Column("server_playlist_id", sa.String(64), nullable=True),
        sa.Column("next_build_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_built_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entries", sa.JSON(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_radio_playlists"),
        sa.UniqueConstraint("user_id", "name", name="uq_radio_playlists_user_id"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_radio_playlists_user_id_users",
            ondelete="CASCADE",
        ),
        sqlite_autoincrement=True,
    )
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":  # Postgres sequences never reuse values
        return
    # The rebuild drops discovery_playlists, which provisional_holds references; SQLite only
    # lets foreign keys be switched off outside a transaction. Ids are copied unchanged, and
    # the copy sets each table's sqlite_sequence to its highest id.
    ctx = op.get_context()
    with ctx.autocommit_block():
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    for table in SEEDS:
        with op.batch_alter_table(
            table, recreate="always", table_kwargs={"sqlite_autoincrement": True}
        ):
            pass
    with ctx.autocommit_block():  # commits the rebuild, then restores enforcement
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    """AUTOINCREMENT stays: harmless on the old schema and the rebuild is not worth repeating."""
    op.drop_table("radio_playlists")
