"""migrations table and requests.migration_id

Revision ID: 0008_migrations
Revises: 0007_plans
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_migrations"
down_revision: str | None = "0007_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "migrations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("manifest_path", sa.String(500), nullable=False),
        sa.Column("verify", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_migrations"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_migrations_user_id_users"),
    )
    with op.batch_alter_table("requests") as b:
        b.add_column(sa.Column("migration_id", sa.Integer(), nullable=True))
        b.create_foreign_key(
            "fk_requests_migration_id_migrations", "migrations", ["migration_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("requests") as b:
        b.drop_constraint("fk_requests_migration_id_migrations", type_="foreignkey")
        b.drop_column("migration_id")
    op.drop_table("migrations")
