"""mcq run token usage + estimated cost

Revision ID: 0017_mcq_run_cost
Revises: 0016_course_collaborators
Create Date: 2026-06-30 00:00:00.000000

Adds summary columns to mcq_runs for observability: total_tokens (input + output)
and estimated_cost_usd (a list-price ESTIMATE; the full per-model/per-step breakdown
lives in result["cost"]). Backfilled to 0 for existing rows — no token data was
captured before this feature, so historical runs show 0.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0017_mcq_run_cost'
down_revision: Union[str, None] = '0016_course_collaborators'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('mcq_runs', sa.Column('total_tokens', sa.Integer(),
                                        nullable=False, server_default='0'))
    op.add_column('mcq_runs', sa.Column('estimated_cost_usd', sa.Float(),
                                        nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('mcq_runs', 'estimated_cost_usd')
    op.drop_column('mcq_runs', 'total_tokens')
