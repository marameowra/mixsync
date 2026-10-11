"""Postgres: move the roles id sequence past the roles 0002 seeded with explicit ids

Revision ID: 0011_roles_sequence
Revises: 0010_decision_created_at_index
"""

from alembic import op

revision = "0011_roles_sequence"
down_revision: str | None = "0010_decision_created_at_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # explicit ids leave the sequence at 1, so the next role insert hits a duplicate key
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "SELECT setval(pg_get_serial_sequence('roles', 'id'), (SELECT max(id) FROM roles))"
        )


def downgrade() -> None:
    pass  # a sequence past max(id) is harmless; nothing to undo
