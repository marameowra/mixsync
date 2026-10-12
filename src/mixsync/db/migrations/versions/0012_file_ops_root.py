"""file_ops.root: which FileOps root (library / discover) an op's relative paths belong to

Revision ID: 0012_file_ops_root
Revises: 0011_roles_sequence
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_file_ops_root"
down_revision: str | None = "0011_roles_sequence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "file_ops",
        sa.Column("root", sa.String(16), nullable=False, server_default="library"),
    )


def downgrade() -> None:
    with op.batch_alter_table("file_ops") as b:
        b.drop_column("root")
