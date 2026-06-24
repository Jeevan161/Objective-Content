"""course owner (created_by)

Revision ID: 0010_course_owner
Revises: 0009_app_feedback
Create Date: 2026-06-24

Adds `created_by` to `courses` — the user who first added (synced) the course. Gates
who may generate MCQs for it. Existing rows are backfilled from the earliest SyncJob
for that course (its `created_by`); courses with no attributable sync stay NULL (open).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_course_owner"
down_revision: Union[str, None] = "0009_app_feedback"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("courses", sa.Column("created_by", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_courses_created_by_users", "courses", "users",
                          ["created_by"], ["id"], ondelete="SET NULL")
    op.create_index("ix_courses_created_by", "courses", ["created_by"])
    # Backfill: earliest sync job's creator per course.
    op.execute("""
        UPDATE courses AS c SET created_by = s.created_by
        FROM (
            SELECT DISTINCT ON (course_id) course_id, created_by
            FROM sync_jobs
            WHERE created_by IS NOT NULL
            ORDER BY course_id, created_at
        ) AS s
        WHERE c.course_id = s.course_id AND c.created_by IS NULL
    """)


def downgrade() -> None:
    op.drop_index("ix_courses_created_by", table_name="courses")
    op.drop_constraint("fk_courses_created_by_users", "courses", type_="foreignkey")
    op.drop_column("courses", "created_by")
