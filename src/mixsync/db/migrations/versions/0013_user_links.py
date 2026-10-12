"""user_links: per-user ListenBrainz and Navidrome accounts

Revision ID: 0013_user_links
Revises: 0012_file_ops_root
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_user_links"
down_revision: str | None = "0012_file_ops_root"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("service", sa.String(20), nullable=False),
        sa.Column("external_username", sa.String(255), nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column("needs_relink", sa.Boolean(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_user_links"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_links_user_id_users", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("user_id", "service", name="uq_user_links_user_id"),
    )


def downgrade() -> None:
    op.drop_table("user_links")
