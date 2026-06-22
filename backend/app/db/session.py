"""
app/db/session.py
-----------------
Synchronous SQLAlchemy engine + session factory.

Sync (not async) keeps Alembic, the portal scraping (blocking requests), and the
thread-based job runner simple; FastAPI runs sync route handlers in its threadpool.
Background job threads each open their own SessionLocal() and close it when done
(mirrors the Django app's connection.close() per thread).
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

# Pool sized for the threaded job runner: each MCQ job fans out per-LO worker
# threads (pmap) that each open a short-lived session, plus progress flushes — a
# small default pool is exhausted by a few concurrent runs. Sizes come from
# settings so they can be tuned per deployment.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yield a session and always close it."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
