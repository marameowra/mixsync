"""watchlist: follows, watched entities, follow hits, requests.source

Revision ID: 0009_watchlist
Revises: 0008_migrations
"""

from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "0009_watchlist"
down_revision: str | None = "0008_migrations"
branch_labels = None
depends_on = None


def _stamps() -> list[sa.Column[datetime]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "watched_entities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("mbid", sa.String(36), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_release_date", sa.String(10), nullable=True),
        *_stamps(),
        sa.PrimaryKeyConstraint("id", name="pk_watched_entities"),
        sa.UniqueConstraint("kind", "mbid", name="uq_watched_entities_kind"),
    )
    op.create_table(
        "follows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("release_types", sa.JSON(), nullable=False),
        sa.Column("only_after_follow", sa.Boolean(), nullable=False),
        sa.Column("min_date", sa.String(10), nullable=True),
        sa.Column("weekly_cap", sa.Integer(), nullable=True),
        *_stamps(),
        sa.PrimaryKeyConstraint("id", name="pk_follows"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_follows_user_id_users"),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["watched_entities.id"], name="fk_follows_entity_id_watched_entities"
        ),
        sa.UniqueConstraint("user_id", "entity_id", name="uq_follows_user_id"),
    )
    op.create_table(
        "follow_hits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("follow_id", sa.Integer(), nullable=False),
        sa.Column("release_group_mbid", sa.String(36), nullable=False),
        sa.Column("release_mbid", sa.String(36), nullable=False),
        sa.Column("artist", sa.String(255), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("release_date", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=True),
        *_stamps(),
        sa.PrimaryKeyConstraint("id", name="pk_follow_hits"),
        sa.ForeignKeyConstraint(
            ["follow_id"], ["follows.id"], name="fk_follow_hits_follow_id_follows"
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["requests.id"], name="fk_follow_hits_request_id_requests"
        ),
        sa.UniqueConstraint("follow_id", "release_group_mbid", name="uq_follow_hits_follow_id"),
    )
    with op.batch_alter_table("requests") as b:
        b.add_column(sa.Column("source", sa.String(16), nullable=False, server_default="manual"))


def downgrade() -> None:
    with op.batch_alter_table("requests") as b:
        b.drop_column("source")
    op.drop_table("follow_hits")
    op.drop_table("follows")
    op.drop_table("watched_entities")
