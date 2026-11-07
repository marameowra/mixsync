"""tracks.path_template: the template each file's path was rendered with (ADR 0019)

Revision ID: 0026_track_path_template
Revises: 0025_app_settings
"""

import sqlalchemy as sa
from alembic import op

revision = "0026_track_path_template"
down_revision: str | None = "0025_app_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rows stay null here: the worker fills them with the configured template at startup.
    with op.batch_alter_table("tracks") as b:
        b.add_column(sa.Column("path_template", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tracks") as b:
        b.drop_column("path_template")
