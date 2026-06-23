"""mcq node-execution traces (own tracing; replaces LangSmith)

Revision ID: 0006_mcq_traces
Revises: 0005_llm_providers
Create Date: 2026-06-23

One row per node ENTRY of an MCQ pipeline run (job_id = the LangGraph checkpoint thread_id).
The repair loop and HITL pause/resume re-enter nodes, so a node may have several spans.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_mcq_traces"
down_revision: Union[str, None] = "0005_llm_providers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mcq_traces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("node", sa.String(64), nullable=False),
        sa.Column("label", sa.String(160), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_mcq_traces_job", "mcq_traces", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_mcq_traces_job", table_name="mcq_traces")
    op.drop_table("mcq_traces")
