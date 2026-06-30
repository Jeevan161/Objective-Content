"""beta_loads unlock_id

Revision ID: 0014_beta_load_unlock_id
Revises: 0013_beta_load_content_job
Create Date: 2026-06-28

Stores the unlock-resources task id alongside the content-loading request id, so the Loads
view can link to BOTH beta-admin tasks (content loading + unlock resource).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_beta_load_unlock_id"
down_revision: Union[str, None] = "0013_beta_load_content_job"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "beta_loads",
        sa.Column("unlock_id", sa.String(length=64), nullable=False, server_default=""),
    )
    op.alter_column("beta_loads", "unlock_id", server_default=None)


def downgrade() -> None:
    op.drop_column("beta_loads", "unlock_id")
