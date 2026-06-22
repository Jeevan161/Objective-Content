"""add unit_parts.resource_ids (learning_resource ids captured at extraction)

Revision ID: 0002_unit_part_resource_ids
Revises: 0001_initial
Create Date: 2026-06-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_unit_part_resource_ids"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "unit_parts",
        sa.Column(
            "resource_ids",
            sa.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'::varchar[]"),
        ),
    )


def downgrade() -> None:
    op.drop_column("unit_parts", "resource_ids")
