"""course question_domain

Revision ID: 0011_course_question_domain
Revises: 0010_course_owner
Create Date: 2026-06-26

Adds `question_domain` to `courses` — a per-course MCQ generation domain (e.g. "SQL").
Empty string = generic. Set once per course; the value is read into the run-scoped
RagAdapter and deterministically activates domain-specific generation/review rules for
the whole run, instead of guessing the domain per learning outcome.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_course_question_domain"
down_revision: Union[str, None] = "0010_course_owner"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "courses",
        sa.Column("question_domain", sa.String(length=16), nullable=False, server_default=""),
    )
    # Drop the server_default now that existing rows are backfilled to '' — the model
    # default ('') governs new inserts at the application layer.
    op.alter_column("courses", "question_domain", server_default=None)


def downgrade() -> None:
    op.drop_column("courses", "question_domain")
