"""course collaborators (per-course access grants)

Revision ID: 0016_course_collaborators
Revises: 0015_classroom_quiz
Create Date: 2026-06-30 00:00:00.000000

Adds the course_collaborators table: a course owner (courses.created_by) or an
admin can grant another user access to work on a course. The grant is immediate
(no approval step). Effective access = owner OR a row here OR an admin.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0016_course_collaborators'
down_revision: Union[str, None] = '0015_classroom_quiz'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'course_collaborators',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('course_id', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('granted_by', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['course_id'], ['courses.course_id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['granted_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('course_id', 'user_id', name='uq_course_collaborator'),
    )
    op.create_index(op.f('ix_course_collaborators_course_id'), 'course_collaborators', ['course_id'], unique=False)
    op.create_index(op.f('ix_course_collaborators_user_id'), 'course_collaborators', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_course_collaborators_user_id'), table_name='course_collaborators')
    op.drop_index(op.f('ix_course_collaborators_course_id'), table_name='course_collaborators')
    op.drop_table('course_collaborators')
