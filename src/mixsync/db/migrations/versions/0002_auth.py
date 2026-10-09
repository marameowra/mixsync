"""users, roles, sessions, matching profiles

Revision ID: 0002_auth
Revises: 0001_initial
"""

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

from mixsync.core.capabilities import DEFAULT_ROLES

revision = "0002_auth"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _ts() -> list[sa.Column[Any]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "matching_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("preset", sa.String(20), nullable=False),
        sa.Column("auto_accept_max", sa.Float(), nullable=False),
        sa.Column("review_max", sa.Float(), nullable=False),
        sa.Column("quality_pref", sa.JSON(), nullable=True),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name="pk_matching_profiles"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("matching_profile_id", sa.Integer(), nullable=True),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.ForeignKeyConstraint(
            ["matching_profile_id"],
            ["matching_profiles.id"],
            name="fk_users_matching_profile_id_matching_profiles",
        ),
    )
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(50), nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name="pk_roles"),
        sa.UniqueConstraint("name", name="uq_roles_name"),
    )
    op.create_table(
        "role_capabilities",
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("capability", sa.String(50), nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("role_id", "capability", name="pk_role_capabilities"),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name="fk_role_capabilities_role_id_roles"
        ),
    )
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        *_ts(),
        sa.PrimaryKeyConstraint("user_id", "role_id", name="pk_user_roles"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_user_roles_user_id_users"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], name="fk_user_roles_role_id_roles"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_agent", sa.String(255), nullable=True),
        *_ts(),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_sessions_user_id_users"),
    )

    now = datetime.now(UTC)
    roles = sa.table(
        "roles",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    caps = sa.table(
        "role_capabilities",
        sa.column("role_id", sa.Integer),
        sa.column("capability", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    for i, (name, bundle) in enumerate(DEFAULT_ROLES.items(), start=1):
        op.bulk_insert(roles, [{"id": i, "name": name, "created_at": now, "updated_at": now}])
        op.bulk_insert(
            caps,
            [
                {"role_id": i, "capability": c.value, "created_at": now, "updated_at": now}
                for c in sorted(bundle)
            ],
        )


def downgrade() -> None:
    for t in ("sessions", "user_roles", "role_capabilities", "roles", "users", "matching_profiles"):
        op.drop_table(t)
