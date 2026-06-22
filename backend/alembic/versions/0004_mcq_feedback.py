"""human-in-the-loop: question feedback + run review_status

Revision ID: 0004_mcq_feedback
Revises: 0003_mcq_runs
Create Date: 2026-06-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_mcq_feedback"
down_revision: Union[str, None] = "0003_mcq_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mcq_runs",
        sa.Column("review_status", sa.String(20), nullable=False, server_default="draft"),
    )
    op.create_table(
        "mcq_question_feedback",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(),
                  sa.ForeignKey("mcq_runs.id", ondelete="CASCADE"), nullable=True),
        sa.Column("stage", sa.String(16), nullable=False, server_default="question"),
        sa.Column("outcome", sa.String(160), nullable=False, server_default=""),
        sa.Column("question_type", sa.String(48), nullable=False, server_default=""),
        sa.Column("action", sa.String(24), nullable=False, server_default=""),
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("before_snapshot", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("after_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("reviewer", sa.String(120), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_mcq_feedback_run", "mcq_question_feedback", ["run_id"])
    op.create_index("ix_mcq_feedback_outcome", "mcq_question_feedback", ["outcome"])


def downgrade() -> None:
    op.drop_index("ix_mcq_feedback_outcome", table_name="mcq_question_feedback")
    op.drop_index("ix_mcq_feedback_run", table_name="mcq_question_feedback")
    op.drop_table("mcq_question_feedback")
    op.drop_column("mcq_runs", "review_status")
