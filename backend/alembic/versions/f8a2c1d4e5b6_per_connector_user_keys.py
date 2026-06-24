"""per-connector user LLM keys (replace users.api_key_enc with user_llm_keys)

Revision ID: f8a2c1d4e5b6
Revises: f7d14e16ade7
Create Date: 2026-06-24 02:10:00.000000

A user now stores one API key PER LLM connector (user_llm_keys), not a single
personal key. Drops the now-unused users.api_key_enc column.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f8a2c1d4e5b6'
down_revision: Union[str, None] = 'f7d14e16ade7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_llm_keys',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('provider_id', sa.Uuid(), nullable=False),
        sa.Column('api_key_enc', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['provider_id'], ['llm_providers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'provider_id', name='uq_user_llm_key'),
    )
    op.create_index(op.f('ix_user_llm_keys_user_id'), 'user_llm_keys', ['user_id'], unique=False)
    op.create_index(op.f('ix_user_llm_keys_provider_id'), 'user_llm_keys', ['provider_id'], unique=False)

    op.drop_column('users', 'api_key_enc')


def downgrade() -> None:
    op.add_column('users', sa.Column('api_key_enc', sa.Text(), server_default='', nullable=False))
    op.drop_index(op.f('ix_user_llm_keys_provider_id'), table_name='user_llm_keys')
    op.drop_index(op.f('ix_user_llm_keys_user_id'), table_name='user_llm_keys')
    op.drop_table('user_llm_keys')
