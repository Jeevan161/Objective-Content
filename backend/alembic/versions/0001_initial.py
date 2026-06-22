"""initial schema: courses hierarchy + sync jobs + pgvector rag_chunks

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

from app.core.config import settings

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # pgvector extension must exist before the vector column is created.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "courses",
        sa.Column("course_id", sa.String(64), primary_key=True),
        sa.Column("environment", sa.String(16), nullable=False, server_default="PROD"),
        sa.Column("course_name", sa.String(500), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("duration", sa.String(64), nullable=False, server_default=""),
        sa.Column("multimedia_url", sa.String(1000), nullable=False, server_default=""),
        sa.Column("course_category", sa.String(255), nullable=False, server_default=""),
        sa.Column("course_link", sa.String(1000), nullable=False, server_default=""),
        sa.Column("selected_courseversion_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("selected_version_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("is_latest_version", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_extracted_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "course_prerequisites",
        sa.Column("course_id", sa.String(64),
                  sa.ForeignKey("courses.course_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("prerequisite_id", sa.String(64),
                  sa.ForeignKey("courses.course_id", ondelete="CASCADE"), primary_key=True),
    )

    op.create_table(
        "topics",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("course_id", sa.String(64),
                  sa.ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False),
        sa.Column("topic_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("topic_name", sa.String(500), nullable=False, server_default=""),
        sa.Column("topic_link", sa.String(1000), nullable=False, server_default=""),
        sa.Column("order", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "units",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("topic_id", sa.Uuid(),
                  sa.ForeignKey("topics.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="SINGLE"),
        sa.Column("label", sa.String(500), nullable=False, server_default=""),
        sa.Column("order", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "unit_parts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("container_id", sa.Uuid(),
                  sa.ForeignKey("units.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(64), nullable=False, server_default=""),
        sa.Column("unit_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("unit_type", sa.String(64), nullable=False, server_default=""),
        sa.Column("name", sa.String(500), nullable=False, server_default=""),
        sa.Column("link", sa.String(1000), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("content_status", sa.String(16), nullable=False, server_default=""),
        sa.Column("content_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("content_extracted_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "sync_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_type", sa.String(16), nullable=False, server_default="SYNC"),
        sa.Column("course_id", sa.String(64), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False, server_default="PROD"),
        sa.Column("prerequisite_for", sa.String(64), nullable=False, server_default=""),
        sa.Column("courseversion_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("version_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("is_latest_version", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "rag_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("unit_part_id", sa.Uuid(),
                  sa.ForeignKey("unit_parts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("course_id", sa.String(64),
                  sa.ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False),
        sa.Column("topic_id", sa.Uuid(),
                  sa.ForeignKey("topics.id", ondelete="CASCADE"), nullable=False),
        sa.Column("unit_id", sa.Uuid(),
                  sa.ForeignKey("units.id", ondelete="CASCADE"), nullable=False),
        sa.Column("section", sa.String(512), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("has_code", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("char_len", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(settings.embed_dimensions), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_rag_chunks_course", "rag_chunks", ["course_id"])
    op.create_index("ix_rag_chunks_course_topic", "rag_chunks", ["course_id", "topic_id"])
    op.create_index("ix_rag_chunks_course_unit", "rag_chunks", ["course_id", "unit_id"])
    # HNSW cosine index for fast approximate nearest-neighbour retrieval.
    op.execute(
        "CREATE INDEX ix_rag_chunks_embedding ON rag_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_table("rag_chunks")
    op.drop_table("sync_jobs")
    op.drop_table("unit_parts")
    op.drop_table("units")
    op.drop_table("topics")
    op.drop_table("course_prerequisites")
    op.drop_table("courses")
    op.execute("DROP EXTENSION IF EXISTS vector")
