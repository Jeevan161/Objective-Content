"""sync_jobs question_domain

Revision ID: 0012_syncjob_question_domain
Revises: 0011_course_question_domain
Create Date: 2026-06-26

Carries the per-course MCQ generation domain (e.g. "SQL"), chosen at course-add time,
on the sync job so it can be written to the Course when the sync persists it.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_syncjob_question_domain"
down_revision: Union[str, None] = "0011_course_question_domain"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sync_jobs",
        sa.Column("question_domain", sa.String(length=16), nullable=False, server_default=""),
    )
    op.alter_column("sync_jobs", "question_domain", server_default=None)


def downgrade() -> None:
    op.drop_column("sync_jobs", "question_domain")
