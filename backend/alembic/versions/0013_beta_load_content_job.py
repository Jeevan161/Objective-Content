"""beta_loads content snapshot + job link

Revision ID: 0013_beta_load_content_job
Revises: 0012_syncjob_question_domain
Create Date: 2026-06-28

Adds two nullable columns to `beta_loads` so a load/export can be tracked as a background
job and its exact loaded question set viewed later:
- `job_id`  — soft link to the SyncJob (job_type LOAD/EXPORT) that performed the action.
- `content` — JSONB snapshot of the {**result, "questions": [...]} payload that was loaded.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_beta_load_content_job"
down_revision: Union[str, None] = "0012_syncjob_question_domain"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("beta_loads", sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("beta_loads", sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_index("ix_beta_loads_job_id", "beta_loads", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_beta_loads_job_id", table_name="beta_loads")
    op.drop_column("beta_loads", "content")
    op.drop_column("beta_loads", "job_id")
