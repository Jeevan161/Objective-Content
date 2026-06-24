"""mcq run version + per-question approval count

Revision ID: 0008_mcq_run_version_approval
Revises: f8a2c1d4e5b6
Create Date: 2026-06-24

Adds two summary columns to mcq_runs:
  * version        — 1-based generation version within a (course_id, unit_id) session
                     (v1 = oldest), so the multiple runs of one session are distinguishable.
  * approved_count — how many generated questions a human has explicitly approved; drives
                     the "load only after approval" gate.
Existing rows are backfilled: version by created_at order within each session, approved
count left at 0 (nothing was approved under the old, ungated flow).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_mcq_run_version_approval"
down_revision: Union[str, None] = "f8a2c1d4e5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("mcq_runs", sa.Column("approved_count", sa.Integer(),
                  nullable=False, server_default="0"))
    op.add_column("mcq_runs", sa.Column("version", sa.Integer(),
                  nullable=False, server_default="1"))
    # Backfill version per (course_id, unit_id) ordered by created_at (oldest = v1).
    op.execute("""
        UPDATE mcq_runs AS m SET version = s.rn
        FROM (
            SELECT id, row_number() OVER (
                PARTITION BY course_id, unit_id ORDER BY created_at
            ) AS rn
            FROM mcq_runs
        ) AS s
        WHERE m.id = s.id
    """)


def downgrade() -> None:
    op.drop_column("mcq_runs", "version")
    op.drop_column("mcq_runs", "approved_count")
