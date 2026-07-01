"""course child order (from details fetch) + learning-set slide urls

Revision ID: 0018_course_child_order_slides
Revises: 0017_mcq_run_cost
Create Date: 2026-07-01 00:00:00.000000

Preserves the authoritative child order from GET_UNIT_RESOURCE_DETAILS (topic_order /
unit_order) instead of the enumerated sync position, and persists each learning set's
published slide-deck URLs (slide_urls) so the course view can embed them.

`child_order` is nullable (populated during extraction; the view falls back to the
enumerated `order` when absent). `slide_urls` defaults to an empty array.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0018_course_child_order_slides'
down_revision: Union[str, None] = '0017_mcq_run_cost'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('topics', sa.Column('child_order', sa.Integer(), nullable=True))
    op.add_column('units', sa.Column('child_order', sa.Integer(), nullable=True))
    op.add_column('unit_parts', sa.Column('child_order', sa.Integer(), nullable=True))
    op.add_column('unit_parts', sa.Column(
        'slide_urls', postgresql.ARRAY(sa.String()),
        nullable=False, server_default='{}'))


def downgrade() -> None:
    op.drop_column('unit_parts', 'slide_urls')
    op.drop_column('unit_parts', 'child_order')
    op.drop_column('units', 'child_order')
    op.drop_column('topics', 'child_order')
