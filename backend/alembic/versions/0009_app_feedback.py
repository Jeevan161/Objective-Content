"""application-level feedback submissions

Revision ID: 0009_app_feedback
Revises: 0008_mcq_run_version_approval
Create Date: 2026-06-24

Creates `app_feedback`: an emoji rating (1–5), a category, an optional 'was this
helpful?' vote, and free text, submitted by any signed-in user and surfaced in the
admin dashboard. Distinct from `mcq_question_feedback` (per-question review actions).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_app_feedback"
down_revision: Union[str, None] = "0008_mcq_run_version_approval"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_feedback",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("category", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("rating", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("helpful", sa.Boolean(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_app_feedback_user_id", "app_feedback", ["user_id"])
    op.create_index("ix_app_feedback_category", "app_feedback", ["category"])


def downgrade() -> None:
    op.drop_index("ix_app_feedback_category", table_name="app_feedback")
    op.drop_index("ix_app_feedback_user_id", table_name="app_feedback")
    op.drop_table("app_feedback")
