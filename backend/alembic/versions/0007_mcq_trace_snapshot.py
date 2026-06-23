"""mcq trace per-node state snapshot

Revision ID: 0007_mcq_trace_snapshot
Revises: 0006_mcq_traces
Create Date: 2026-06-23

Adds a compact JSONB `snapshot` to each node-execution span so the UI can show what a node
produced (counts + small samples) when the node is expanded. Deliberately small — NOT the full
LangGraph checkpoint state.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_mcq_trace_snapshot"
down_revision: Union[str, None] = "0006_mcq_traces"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mcq_traces",
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("mcq_traces", "snapshot")
