"""mirrored playlists: when the owner turned on auto-requesting (ListenBrainz loves)

Revision ID: 0031_mirror_auto_request
Revises: 0030_mirrored_playlists
"""

import sqlalchemy as sa
from alembic import op

revision = "0031_mirror_auto_request"
down_revision: str | None = "0030_mirrored_playlists"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("mirrored_playlists") as batch:
        batch.add_column(sa.Column("auto_request_since", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("mirrored_playlists") as batch:
        batch.drop_column("auto_request_since")
