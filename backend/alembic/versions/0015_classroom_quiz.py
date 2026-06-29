"""classroom quiz decks + scopes + mcq_runs.reading_material

Revision ID: 0015_classroom_quiz
Revises: 0014_beta_load_unlock_id
Create Date: 2026-06-29

Adds the Classroom Quiz ingest tables (a published Slides deck segmented into per-quiz
scopes) and a `reading_material` column on `mcq_runs` so a Classroom Quiz scope can carry
the session handout generated for it (portal MCQ runs leave it empty).

No change needed for `sync_jobs.job_type` — the new CLASSROOM_QUIZ value fits the existing
String(16) column.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_classroom_quiz"
down_revision: Union[str, None] = "0014_beta_load_unlock_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("mcq_runs", sa.Column("reading_material", sa.Text(), nullable=False,
                                        server_default=""))

    op.create_table(
        "classroom_quiz_decks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slides_url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="SCOPED"),
        sa.Column("scope_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("question_domain", sa.String(16), nullable=False, server_default=""),
        sa.Column("created_by", sa.Uuid(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_classroom_quiz_decks_status", "classroom_quiz_decks", ["status"])
    op.create_index("ix_classroom_quiz_decks_created_by", "classroom_quiz_decks", ["created_by"])

    op.create_table(
        "classroom_quiz_scopes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("deck_id", sa.Uuid(),
                  sa.ForeignKey("classroom_quiz_decks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scope_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False, server_default=""),
        sa.Column("slide_start", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("slide_end", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("slide_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("reading_material", sa.Text(), nullable=False, server_default=""),
        sa.Column("coverage", sa.String(16), nullable=False, server_default="OK"),
        sa.Column("run_id", sa.Uuid(),
                  sa.ForeignKey("mcq_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("deck_id", "scope_no", name="uq_cq_scope_deck_no"),
    )
    op.create_index("ix_classroom_quiz_scopes_deck", "classroom_quiz_scopes", ["deck_id"])
    op.create_index("ix_classroom_quiz_scopes_run", "classroom_quiz_scopes", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_classroom_quiz_scopes_run", table_name="classroom_quiz_scopes")
    op.drop_index("ix_classroom_quiz_scopes_deck", table_name="classroom_quiz_scopes")
    op.drop_table("classroom_quiz_scopes")
    op.drop_index("ix_classroom_quiz_decks_created_by", table_name="classroom_quiz_decks")
    op.drop_index("ix_classroom_quiz_decks_status", table_name="classroom_quiz_decks")
    op.drop_table("classroom_quiz_decks")
    op.drop_column("mcq_runs", "reading_material")
