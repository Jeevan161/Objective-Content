"""user-configurable LLM providers (encrypted keys)

Revision ID: 0005_llm_providers
Revises: 0004_mcq_feedback
Create Date: 2026-06-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_llm_providers"
down_revision: Union[str, None] = "0004_mcq_feedback"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_providers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("adapter", sa.String(32), nullable=False, server_default="openai_compatible"),
        sa.Column("model", sa.String(128), nullable=False, server_default=""),
        sa.Column("base_url", sa.String(500), nullable=False, server_default=""),
        sa.Column("api_key_enc", sa.Text(), nullable=False, server_default=""),
        sa.Column("default_headers", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("extra_body", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_llm_providers_name", "llm_providers", ["name"], unique=True)
    op.create_index("ix_llm_providers_active", "llm_providers", ["active"])


def downgrade() -> None:
    op.drop_index("ix_llm_providers_active", table_name="llm_providers")
    op.drop_index("ix_llm_providers_name", table_name="llm_providers")
    op.drop_table("llm_providers")
