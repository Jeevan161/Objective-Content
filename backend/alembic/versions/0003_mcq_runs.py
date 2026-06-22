"""mcq runs + editable prompts + sync_jobs.progress

Revision ID: 0003_mcq_runs
Revises: 0002_unit_part_resource_ids
Create Date: 2026-06-16

Prompts are seeded from code defaults at app startup (see app/main.py), after the
tables exist — not here — to avoid importing the pipeline (langgraph) inside the
migration transaction.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_mcq_runs"
down_revision: Union[str, None] = "0002_unit_part_resource_ids"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Structured live progress for the MCQ pipeline.
    op.add_column("sync_jobs", sa.Column("progress", postgresql.JSONB(), nullable=True))

    op.create_table(
        "mcq_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(),
                  sa.ForeignKey("sync_jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("course_id", sa.String(64), nullable=False),
        sa.Column("topic_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("unit_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("langsmith_run_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("lo_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("question_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("needs_human_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_mcq_runs_course", "mcq_runs", ["course_id"])
    op.create_index("ix_mcq_runs_unit", "mcq_runs", ["unit_id"])

    op.create_table(
        "mcq_prompts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("key", "version", name="uq_mcq_prompt_key_version"),
    )
    op.create_index("ix_mcq_prompts_key", "mcq_prompts", ["key"])


def downgrade() -> None:
    op.drop_index("ix_mcq_prompts_key", table_name="mcq_prompts")
    op.drop_table("mcq_prompts")
    op.drop_index("ix_mcq_runs_unit", table_name="mcq_runs")
    op.drop_index("ix_mcq_runs_course", table_name="mcq_runs")
    op.drop_table("mcq_runs")
    op.drop_column("sync_jobs", "progress")
