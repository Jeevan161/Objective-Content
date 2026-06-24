"""auth users, per-user api keys, task logs, attribution

Revision ID: f7d14e16ade7
Revises: 0007_mcq_trace_snapshot
Create Date: 2026-06-24 01:20:14.108573

Hand-trimmed from autogenerate: ONLY the intended additions (users / task_logs /
beta_loads tables + created_by attribution columns). Autogenerate's other diffs
(checkpoint-table drops, the HNSW embedding index, index renames, NOT-NULL flips)
were noise/destructive and intentionally excluded.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f7d14e16ade7'
down_revision: Union[str, None] = '0007_mcq_trace_snapshot'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('password_hash', sa.Text(), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('api_key_enc', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_index(op.f('ix_users_is_active'), 'users', ['is_active'], unique=False)

    op.create_table(
        'task_logs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('job_id', sa.Uuid(), nullable=True),
        sa.Column('run_id', sa.Uuid(), nullable=True),
        sa.Column('user_id', sa.Uuid(), nullable=True),
        sa.Column('task_type', sa.String(length=16), nullable=False),
        sa.Column('level', sa.String(length=8), nullable=False),
        sa.Column('event', sa.String(length=64), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_task_logs_job', 'task_logs', ['job_id'], unique=False)
    op.create_index('ix_task_logs_created', 'task_logs', ['created_at'], unique=False)
    op.create_index(op.f('ix_task_logs_user_id'), 'task_logs', ['user_id'], unique=False)

    op.create_table(
        'beta_loads',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('run_id', sa.Uuid(), nullable=True),
        sa.Column('user_id', sa.Uuid(), nullable=True),
        sa.Column('action', sa.String(length=8), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('resource_id', sa.String(length=64), nullable=False),
        sa.Column('sheet_url', sa.Text(), nullable=False),
        sa.Column('s3_url', sa.Text(), nullable=False),
        sa.Column('request_id', sa.String(length=64), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['mcq_runs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_beta_loads_run_id'), 'beta_loads', ['run_id'], unique=False)
    op.create_index(op.f('ix_beta_loads_user_id'), 'beta_loads', ['user_id'], unique=False)

    # Attribution columns (nullable so existing rows are fine).
    op.add_column('sync_jobs', sa.Column('created_by', sa.Uuid(), nullable=True))
    op.create_index(op.f('ix_sync_jobs_created_by'), 'sync_jobs', ['created_by'], unique=False)
    op.create_foreign_key('fk_sync_jobs_created_by_users', 'sync_jobs', 'users',
                          ['created_by'], ['id'], ondelete='SET NULL')

    op.add_column('mcq_runs', sa.Column('created_by', sa.Uuid(), nullable=True))
    op.create_index(op.f('ix_mcq_runs_created_by'), 'mcq_runs', ['created_by'], unique=False)
    op.create_foreign_key('fk_mcq_runs_created_by_users', 'mcq_runs', 'users',
                          ['created_by'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint('fk_mcq_runs_created_by_users', 'mcq_runs', type_='foreignkey')
    op.drop_index(op.f('ix_mcq_runs_created_by'), table_name='mcq_runs')
    op.drop_column('mcq_runs', 'created_by')

    op.drop_constraint('fk_sync_jobs_created_by_users', 'sync_jobs', type_='foreignkey')
    op.drop_index(op.f('ix_sync_jobs_created_by'), table_name='sync_jobs')
    op.drop_column('sync_jobs', 'created_by')

    op.drop_index(op.f('ix_beta_loads_user_id'), table_name='beta_loads')
    op.drop_index(op.f('ix_beta_loads_run_id'), table_name='beta_loads')
    op.drop_table('beta_loads')

    op.drop_index(op.f('ix_task_logs_user_id'), table_name='task_logs')
    op.drop_index('ix_task_logs_created', table_name='task_logs')
    op.drop_index('ix_task_logs_job', table_name='task_logs')
    op.drop_table('task_logs')

    op.drop_index(op.f('ix_users_is_active'), table_name='users')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')
